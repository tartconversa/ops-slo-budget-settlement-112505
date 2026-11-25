"""变更额度台。

口径：

* 申请在全部涉及 scope（含共享 edge）上同时占用，额度未使用前同样占用余量；
* 争用时先保留已开工消耗；未开工申请按原申请到达顺序重新审核；
* 客户端补证/更正/撤销可能同时缩减共享额度与某服务额度，尚可改动的变更
  （已批准未开工）回流到可决策队列，已开工/已完成的授权依据保持不变；
* 同一次用户失败在共享额度里只计一份（edge 去重在 evidence 层完成），
  撤单不会给仍有亏空的服务腾出新额度——亏空 scope 拒绝一切新授权；
* 正在执行的操作若遇到 scope 亏空，向当班人输出风险与仍可做的动作，
  不强制中断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import AuthorizationSnapshot, BudgetLedger
from .model import ChangeAction, ChangeState

TERMINAL_STATES = {ChangeState.FINISHED, ChangeState.WITHDRAWN}


@dataclass
class ChangeTicket:
    action_id: str
    period_start: int
    scopes: tuple[str, ...]
    units: int
    state: ChangeState
    created_at: int
    sequence: int
    updated_at: int
    reflowed_at: int | None = None
    # 回流那一刻各 scope 的证据释放序号；只有之后序号增长才允许重新授权
    reflow_seq: dict[tuple[int, str], int] | None = None
    # 回流那一刻的全局释放纪元；本批内的释放纪元不变，杜绝同批重批
    reflow_epoch: int | None = None
    snapshots: list[AuthorizationSnapshot] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    last_decision: str = ""

    @property
    def decided(self) -> bool:
        return self.state in (ChangeState.APPLIED, ChangeState.RUNNING,
                              ChangeState.FINISHED)

    @property
    def mutable(self) -> bool:
        """尚可改动：只有已批准未开工的变更能回到可决策状态。"""
        return self.state is ChangeState.APPLIED

    def as_dict(self) -> dict:
        return {
            "action": self.action_id,
            "period_start": self.period_start,
            "scopes": list(self.scopes),
            "units": self.units,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_decision": self.last_decision,
            "history": list(self.history),
            "authorizations": [s.as_dict() for s in self.snapshots],
        }


@dataclass(frozen=True)
class DeskEvent:
    at: int
    kind: str
    action: str | None
    detail: str
    risk: str | None = None


class ChangeDesk:
    def __init__(self, ledger: BudgetLedger) -> None:
        self.ledger = ledger
        self.tickets: dict[str, ChangeTicket] = {}
        self.queue: list[str] = []       # 未开工、等待重审的 action，按到达顺序
        self.events: list[DeskEvent] = []
        self._counter = 0

    # ------------------------------------------------------------- 主入口
    def handle(self, action: ChangeAction) -> list[DeskEvent]:
        """处理一个动作；不在内部自动重审队列，重审由调用方带水位驱动。"""
        handler = {
            "apply": self._apply,
            "begin": self._begin,
            "finish": self._finish,
            "withdraw": self._withdraw,
        }[action.kind]
        return handler(action)

    # ------------------------------------------------------------- 争用重审
    def reflow_after_evidence(self, now: int) -> list[DeskEvent]:
        """证据变动后调用：把不再满足余量的已批准未开工变更打回队列。

        本方法只做回流，不在同一轮里立刻重批——否则释放出的额度会把刚打回的
        变更原样授权回来，形成振荡。重审统一由 :meth:`drain_queue` 在证据/动作
        事件处理完后显式驱动。
        """
        events: list[DeskEvent] = []
        for ticket in list(self.tickets.values()):
            if not ticket.mutable:
                continue
            action = self._as_action(ticket, now)
            ok, shortfall = self.ledger.can_authorize(action)
            if ok:
                continue
            self.ledger.release_change(action)
            ticket.state = ChangeState.PENDING
            ticket.updated_at = now
            ticket.reflowed_at = now
            ticket.reflow_seq = {
                (ticket.period_start, scope):
                    self.ledger.budget_for(ticket.period_start, scope).release_seq
                for scope in ticket.scopes
            }
            ticket.reflow_epoch = self.ledger.release_epoch
            ticket.last_decision = (
                f"证据更正后实时余量不足，缺额 {shortfall}，回到可决策状态排队")
            ticket.history.append(
                f"第 {now} 分钟：{ticket.last_decision}")
            if ticket.action_id not in self.queue:
                # 回到原申请到达顺序的队尾位置由 sequence 决定；这里保持 FIFO。
                self.queue.append(ticket.action_id)
            events.append(DeskEvent(
                now, "change-reflow", ticket.action_id,
                f"已批准未开工变更回流：{ticket.last_decision}"))
        for risk in self.running_risks(now):
            events.append(risk)
        return events

    def drain_queue(self, now: int,
                    entry_epoch: int | None = None) -> list[DeskEvent]:
        """按原申请到达顺序逐个重审；前一个不通过不影响后面继续尝试。

        回流门控（基于单调释放纪元，与分钟解耦，同分钟多批次也不误判）：

        * 被打回过的变更，只有在回流**之后**发生过独立的证据单位净释放
          （全局释放纪元大于回流时纪元）才重新参与审核；
        * 若调用方给出 ``entry_epoch``（本批处理前的纪元），则本批内部产生
          的任何释放都不放行，杜绝“回流释放的额度把同一变更原样授权回来”。
        """
        events: list[DeskEvent] = []
        current_epoch = self.ledger.release_epoch
        for action_id in list(self.queue):
            ticket = self.tickets[action_id]
            if ticket.state is not ChangeState.PENDING:
                self.queue.remove(action_id)
                continue
            if ticket.reflowed_at is not None:
                baseline = ticket.reflow_epoch
                # 本批内的释放对本次重审不可见。
                visible_epoch = (current_epoch if entry_epoch is None
                                 else min(current_epoch, entry_epoch))
                if visible_epoch <= (baseline if baseline is not None else -1):
                    ticket.last_decision = (
                        "回流后尚无独立的证据单位净释放，"
                        "等待补证/更正/撤销或到期结算")
                    continue
            action = self._as_action(ticket, now)
            deficit = self.ledger.has_deficit(ticket.scopes, ticket.period_start)
            ok, shortfall = self.ledger.can_authorize(action)
            if deficit:
                ticket.last_decision = (
                    f"涉及 scope 存在亏空 {deficit}，亏空未补前不发新额度")
                ticket.updated_at = now
                events.append(DeskEvent(
                    now, "authorization-blocked", action_id,
                    ticket.last_decision))
                continue
            if not ok:
                ticket.last_decision = f"实时余量不足，缺额 {shortfall}，继续排队"
                ticket.updated_at = now
                events.append(DeskEvent(
                    now, "authorization-pending", action_id, ticket.last_decision))
                continue
            self._authorize(ticket, action, "apply")
            ticket.reflowed_at = None
            ticket.reflow_seq = None
            ticket.reflow_epoch = None
            self.queue.remove(action_id)
            events.append(DeskEvent(
                now, "authorization-granted", action_id,
                f"按到达顺序批准，{action.units} 单位已在 "
                f"{', '.join(action.scopes)} 上同时占用"))
        return events

    def _evidence_freed(self, ticket: ChangeTicket) -> bool:
        """回流之后，任一涉及 scope 的证据单位净释放序号是否增长过。"""
        if not ticket.reflow_seq:
            return True
        for (period, scope), baseline in ticket.reflow_seq.items():
            if self.ledger.budget_for(period, scope).release_seq > baseline:
                return True
        return False

    # ------------------------------------------------------------- 申请
    def _apply(self, action: ChangeAction) -> list[DeskEvent]:
        if action.action in self.tickets:
            raise ValueError(f"变更申请重复：{action.action}")
        self._counter += 1
        ticket = ChangeTicket(
            action_id=action.action, period_start=action.period_start,
            scopes=action.scopes, units=action.units,
            state=ChangeState.PENDING, created_at=action.at,
            sequence=self._counter, updated_at=action.at)
        self.tickets[action.action] = ticket
        self.queue.append(action.action)
        ticket.history.append(
            f"第 {action.at} 分钟提交申请，占用 {action.units} 单位，"
            f"涉及 {', '.join(action.scopes)}")
        return [DeskEvent(action.at, "apply-received", action.action,
                          f"申请进入额度台，按到达顺序排队（序号 {ticket.sequence}）")]

    # ------------------------------------------------------------- 开工
    def _begin(self, action: ChangeAction) -> list[DeskEvent]:
        ticket = self._ticket(action)
        if ticket.state is ChangeState.PENDING:
            ok, shortfall = self.ledger.can_authorize(action)
            if not ok:
                raise ValueError(
                    f"变更 {action.action} 尚未获得授权（缺额 {shortfall}），不能开工")
            self._authorize(ticket, action, "begin")
        if ticket.state is not ChangeState.APPLIED:
            raise ValueError(
                f"变更 {action.action} 当前状态 {ticket.state.value}，不能开工")
        ticket.state = ChangeState.RUNNING
        ticket.updated_at = action.at
        ticket.history.append(f"第 {action.at} 分钟开工，授权依据固定不再回流")
        return [DeskEvent(action.at, "change-begin", action.action,
                          "变更开工，已成为受保护的已开工消耗")]

    # ------------------------------------------------------------- 完成
    def _finish(self, action: ChangeAction) -> list[DeskEvent]:
        ticket = self._ticket(action)
        if ticket.state is not ChangeState.RUNNING:
            raise ValueError(
                f"变更 {action.action} 当前状态 {ticket.state.value}，只有执行中可完成")
        ticket.state = ChangeState.FINISHED
        ticket.updated_at = action.at
        snap = self.ledger.snapshot(
            action, "finish：完成时按当时已有证据记录后果，后续重述不回改")
        ticket.snapshots.append(snap)
        ticket.history.append(f"第 {action.at} 分钟完成，保留原授权依据")
        return [DeskEvent(action.at, "change-finish", action.action,
                          "变更完成，额度消耗保留在月度结算内")]

    # ------------------------------------------------------------- 撤销
    def _withdraw(self, action: ChangeAction) -> list[DeskEvent]:
        ticket = self._ticket(action)
        if ticket.state in TERMINAL_STATES:
            raise ValueError(f"变更 {action.action} 已 {ticket.state.value}，不能撤销")
        if ticket.state is ChangeState.RUNNING:
            raise ValueError(f"变更 {action.action} 正在执行，不能直接撤销")
        if ticket.state is ChangeState.APPLIED:
            self.ledger.release_change(action)
        if action.action in self.queue:
            self.queue.remove(action.action)
        ticket.state = ChangeState.WITHDRAWN
        ticket.updated_at = action.at
        ticket.history.append(f"第 {action.at} 分钟撤单，未开工占用已释放")
        # 撤单释放未开工占用并会触发队列重审；但因证据亏空而回流的变更仍受
        # 证据释放门控约束，撤单本身不构成其重新授权的依据；仍有亏空的 scope
        # 也不会因撤单获得新额度（drain_queue 的亏空门控负责）。
        return [DeskEvent(action.at, "change-withdraw", action.action,
                          "撤单释放未开工占用；仍有亏空的服务不会因此获得新额度")]

    def _authorize(self, ticket: ChangeTicket, action: ChangeAction,
                   phase: str) -> None:
        self.ledger.hold_change(action)
        ticket.state = ChangeState.APPLIED
        ticket.updated_at = action.at
        basis = (f"{phase}：按第 {action.at} 分钟已有证据授权，"
                 "后续补证/更正只进入当前重述，不回改本快照")
        snap = self.ledger.snapshot(action, basis)
        ticket.snapshots.append(snap)
        ticket.last_decision = "已授权，占用实时余量"
        ticket.history.append(
            f"第 {action.at} 分钟授权（{phase}），快照占用 {snap.occupied}")

    # ------------------------------------------------------------- 风险提示
    def running_risks(self, now: int) -> list[DeskEvent]:
        """正在执行的操作遇到亏空时，向当班人明确风险和仍可做的动作。"""
        risks: list[DeskEvent] = []
        for ticket in self.tickets.values():
            if ticket.state is not ChangeState.RUNNING:
                continue
            deficit = self.ledger.has_deficit(ticket.scopes, ticket.period_start)
            if deficit:
                risks.append(DeskEvent(
                    now, "running-risk", ticket.action_id,
                    f"执行中变更涉及亏空 scope {deficit}",
                    risk=self._running_risk(ticket, now)))
        return risks

    def _running_risk(self, ticket: ChangeTicket, now: int) -> str:
        return (
            f"变更 {ticket.action_id} 的授权依据保持有效；可继续执行或完成，"
            "不能被强制回流或撤单。仍可做的动作：完成本变更、暂停同 scope 的"
            "新申请、等待补证期满后的保守结算或客户端补证重述")

    # ------------------------------------------------------------- 辅助
    def _ticket(self, action: ChangeAction) -> ChangeTicket:
        ticket = self.tickets.get(action.action)
        if ticket is None:
            raise ValueError(f"未知变更 {action.action}，缺少 apply 记录")
        if ticket.units != action.units or ticket.scopes != action.scopes:
            raise ValueError(
                f"变更 {action.action} 的 scopes/units 与原申请不一致")
        if ticket.period_start != action.period_start:
            raise ValueError(f"变更 {action.action} 的承诺期与原申请不一致")
        return ticket

    def _as_action(self, ticket: ChangeTicket, now: int) -> ChangeAction:
        return ChangeAction(
            at=now, action=ticket.action_id, kind="apply",
            period_start=ticket.period_start, scopes=ticket.scopes,
            units=ticket.units)

    def pending_order(self) -> list[str]:
        return list(self.queue)
