"""事件溯源编排：同一个入口持续接收边缘、客户端和服务结算记录与变更申请。

处理顺序保证口径可复算：

1. 先接纳全部已发布承诺（条款按账期版本化，按请求初次进入时间取条款）；
2. 其余记录按接收时间 ``at``/``minute`` 排序，同类保持资料内顺序；
3. 每条记录处理后推进所有已到期的补证期限，再让额度台按最新余量重审；
4. 所有单位变动、授权、回流、风险都进同一份留痕日志，供跨月对账解释差异。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .desk import ChangeDesk, DeskEvent
from .evidence import EvidenceBoard, JournalEvent
from .ledger import BudgetLedger
from .model import (
    ChangeAction,
    Fact,
    Intake,
    Objective,
)


@dataclass(frozen=True)
class EngineEvent:
    seq: int
    at: int
    stream: str
    kind: str
    ref: str
    detail: str
    delta_fail: dict[str, int] = field(default_factory=dict)
    delta_hold: dict[str, int] = field(default_factory=dict)
    risk: str | None = None
    evidence_ref: tuple[str, str] | None = None

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "stream": self.stream,
            "kind": self.kind,
            "ref": self.ref,
            "detail": self.detail,
            "delta_fail": dict(self.delta_fail),
            "delta_hold": dict(self.delta_hold),
            "risk": self.risk,
            "evidence_ref": list(self.evidence_ref) if self.evidence_ref else None,
        }


class SettlementEngine:
    """组合证据板、预算台账与额度台的增量引擎。"""

    def __init__(self) -> None:
        self.board = EvidenceBoard()
        self.ledger = BudgetLedger()
        self.desk = ChangeDesk(self.ledger)
        self.log: list[EngineEvent] = []
        self._seq = 0
        self._processed: set[tuple[str, str, int]] = set()
        self.now = 0

    # ------------------------------------------------------------- 构建入口
    def load_feeds(self, rows: Iterable[dict[str, Any]]) -> None:
        """从同一个入口接入新一月的资料行（四类记录混排也可）。"""
        objectives: list[Objective] = []
        intakes: list[Intake] = []
        facts: list[Fact] = []
        changes: list[ChangeAction] = []
        for index, row in enumerate(rows):
            feed = row.get("_feed")
            if feed == "objectives":
                objectives.append(Objective.from_row(row))
            elif feed == "intake":
                intakes.append(Intake.from_row(row))
            elif feed == "facts":
                facts.append(Fact.from_row(row))
            elif feed == "changes":
                changes.append(ChangeAction.from_row(row))
            else:
                raise ValueError(f"第 {index + 1} 行缺少有效的 _feed 标记：{row!r}")

        # 承诺先注册；同一账期 scope 重复由下层拒绝
        for objective in objectives:
            self.register_objective(objective)
        # 同分钟内的流优先级：先接纳请求，再落证据事实，最后处理变更动作，
        # 保证授权决策能读到该分钟已对齐的证据。
        stream_rank = {"intake": 0, "fact": 1, "change": 2}
        timeline: list[tuple[int, int, int, str, Any]] = []
        for order, intake in enumerate(intakes):
            timeline.append((intake.minute, stream_rank["intake"], order,
                             "intake", intake))
        for order, fact in enumerate(facts):
            timeline.append((fact.at, stream_rank["fact"], order, "fact", fact))
        for order, action in enumerate(changes):
            timeline.append((action.at, stream_rank["change"], order,
                             "change", action))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))
        for at, _rank, _order, kind, payload in timeline:
            if kind == "intake":
                self.register_intake(payload)
            elif kind == "fact":
                self.register_fact(payload)
            else:
                self.register_change(payload)

    @classmethod
    def replay(cls, rows: Iterable[dict[str, Any]]) -> "SettlementEngine":
        engine = cls()
        engine.load_feeds(list(rows))
        return engine

    # ------------------------------------------------------------- 单条接入
    def register_objective(self, objective: Objective) -> None:
        self.board.register_objective(objective)
        self.ledger.add_objective(objective)

    def register_intake(self, intake: Intake) -> None:
        events = self.board.register_intake(intake)
        self._absorb_evidence(events, intake.minute)
        self.now = max(self.now, intake.minute)

    def register_fact(self, fact: Fact) -> None:
        dedupe = ("fact", f"{fact.source}:{fact.ref}", fact.revision)
        if dedupe in self._processed:
            return
        self._processed.add(dedupe)
        # 入口纪元在本方法任何台账变动之前捕获，杜绝同批释放污染门控。
        entry_epoch = self.ledger.release_epoch
        self._advance(fact.at)
        events = self.board.register_fact(fact)
        # 严格顺序：先把证据板单位全量对齐进台账，再回流、再重审，
        # 保证所有额度决策读到的是同步后的同一组数字。
        self._absorb_evidence(events, fact.at)
        for event in self.desk.reflow_after_evidence(fact.at):
            self._record_desk(event)
        for event in self.desk.drain_queue(fact.at, entry_epoch):
            self._record_desk(event)
        self.now = max(self.now, fact.at)

    def register_change(self, action: ChangeAction) -> None:
        # 同一变更的 apply/begin/finish/withdraw 是合法动作序列，不去重；
        # 非法状态转换由 ChangeDesk 拒绝。
        entry_epoch = self.ledger.release_epoch
        self._advance(action.at)
        for event in self.desk.handle(action):
            self._record_desk(event)
        # 撤单等动作释放的额度可用于重审；纪元门控仍保证证据回流不被同批放大。
        for event in self.desk.drain_queue(action.at, entry_epoch):
            self._record_desk(event)
        self.now = max(self.now, action.at)

    def advance_to(self, minute: int) -> None:
        """供月末结算/演练显式推进时间。"""
        self._advance(minute)
        self.now = max(self.now, minute)

    # ------------------------------------------------------------- 内部
    def _advance(self, now: int) -> None:
        """到期结算：只推进裁决与回流，不在这里重审。

        重审统一由外层（register_fact/register_change）用入口纪元驱动，
        避免到期结算释放的单位在同批把刚回流的变更又授权回来。
        """
        events = self.board.advance(now)
        if not events:
            return
        self._absorb_evidence(events, now)
        for event in self.desk.reflow_after_evidence(now):
            self._record_desk(event)

    def _absorb_evidence(self, events: list[JournalEvent], now: int) -> None:
        """证据板是单位数唯一事实来源：台账证据单位逐 scope 全量对齐并留痕。

        回流门控只依赖单调的释放纪元（release_epoch），与分钟解耦；
        ``last_evidence_release`` 仅作可读的信息字段。
        """
        for event in events:
            if event.scope is None or event.period_start is None:
                raise ValueError("证据事件缺少 scope/账期")
            self._seq += 1
            self.log.append(EngineEvent(
                seq=self._seq, at=event.at, stream="evidence", kind=event.kind,
                ref=event.request or "-", detail=event.detail,
                evidence_ref=event.evidence))
        for period in self.ledger.periods():
            scopes = [scope for (p, scope) in self.ledger.scopes if p == period]
            for scope in scopes:
                fail, hold = self.board.current_units(period, scope)
                budget = self.ledger.budget_for(period, scope)
                delta_fail = fail - budget.failure_units
                delta_hold = hold - budget.reserved_units
                if delta_fail or delta_hold:
                    old_total = budget.failure_units + budget.reserved_units
                    budget.failure_units = fail
                    budget.reserved_units = hold
                    new_total = fail + hold
                    if fail > budget.budget:
                        budget.ever_deficit = True
                    # 只有证据占用真正净减少才算一次释放；fail<->hold 转换不算。
                    if new_total < old_total:
                        budget.release_seq += 1
                        budget.last_evidence_release = now
                        self.ledger.note_evidence_release()
                    self._seq += 1
                    self.log.append(EngineEvent(
                        seq=self._seq, at=now, stream="ledger",
                        kind="unit-sync", ref=scope,
                        detail=(f"{scope} 台账对齐证据板：坏请求={fail}，"
                                f"缺证预留={hold}（差值 失败 {delta_fail:+d}/"
                                f"预留 {delta_hold:+d}）"),
                        delta_fail={scope: delta_fail},
                        delta_hold={scope: delta_hold}))

    def _record_desk(self, event: DeskEvent) -> None:
        # 同一分钟内对同一变更的重复风险/排队提示只留痕一次（不限相邻）。
        for prior in self.log:
            if (prior.at == event.at and prior.stream == "changes"
                    and prior.kind == event.kind and prior.ref == event.action
                    and prior.detail == event.detail):
                return
        self._seq += 1
        self.log.append(EngineEvent(
            seq=self._seq, at=event.at, stream="changes", kind=event.kind,
            ref=event.action or "-", detail=event.detail, risk=event.risk))

    # ------------------------------------------------------------- 快照持久化
    def to_snapshot(self) -> dict[str, Any]:
        """把当前状态序列化到本地演练文件（无任何外部副作用）。"""
        return {
            "version": 1,
            "now": self.now,
            "live_view": {
                f"{period}|{scope}": view
                for (period, scope), view in self.ledger.live_view().items()
            },
            "restated_view": {
                f"{period}|{scope}": view
                for (period, scope), view in self.ledger.restated_view().items()
            },
            "tickets": {
                action_id: ticket.as_dict()
                for action_id, ticket in self.desk.tickets.items()
            },
            "queue": self.desk.pending_order(),
            "verdicts": self._verdict_dump(),
            "log": [event.as_dict() for event in self.log],
        }

    def save_snapshot(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    def _verdict_dump(self) -> dict[str, dict]:
        dump: dict[str, dict] = {}
        for request, intake in self.board.intakes.items():
            services = {}
            for service in intake.services:
                verdict = self.board.service_verdict(request, service)
                services[service] = {
                    "result": verdict.result.value,
                    "basis": verdict.basis.value,
                    "reason": verdict.reason,
                    "at": verdict.at,
                    "restated": verdict.restated,
                    "evidence": list(verdict.evidence) if verdict.evidence else None,
                }
            edge = self.board.edge_verdict(request)
            dump[request] = {
                "period_start": self.board.period_of[request],
                "intake_minute": intake.minute,
                "client_receipt": intake.client_receipt,
                "services": services,
                "edge": {
                    "result": edge.result.value,
                    "basis": edge.basis.value,
                    "reason": edge.reason,
                    "restated": edge.restated,
                },
            }
        return dump
