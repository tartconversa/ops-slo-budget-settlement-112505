"""报表与对账。

刻意不把结算结果压成一个健康比例：发版负责人需要同时看到
实时可用余量、当前重述结算、当时授权依据，以及每条变更当前是否可开工。
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import SettlementEngine
from .model import ChangeState, EDGE_SCOPE


@dataclass(frozen=True)
class ReconciliationItem:
    """一条对账差异：当前重述与当时授权为何对不上。"""

    request: str
    scope: str
    period_start: int
    authorized_as: str
    restated_as: str
    cause: str
    evidence: str | None


class Reporter:
    def __init__(self, engine: SettlementEngine) -> None:
        self.engine = engine
        self.board = engine.board
        self.ledger = engine.ledger
        self.desk = engine.desk

    # ------------------------------------------------------------- 预算总览
    def budget_table(self) -> list[dict]:
        rows: list[dict] = []
        for period in self.ledger.periods():
            scopes = sorted(scope for (p, scope) in self.ledger.scopes if p == period)
            for scope in scopes:
                budget = self.ledger.budget_for(period, scope)
                rows.append({
                    "period_start": period,
                    "scope": scope,
                    "budget": budget.budget,
                    "restated_failures": budget.failure_units,
                    "reserved_holds": budget.reserved_units,
                    "change_committed": budget.change_committed,
                    "live_available": budget.live_available,
                    "deficit": budget.deficit,
                    "ever_deficit": budget.ever_deficit,
                    "healthy_ratio_note": round(budget.healthy_ratio, 3),
                    "decision": self._decision(scope, budget),
                })
        return rows

    def _decision(self, scope: str, budget) -> str:
        if budget.deficit > 0:
            return f"亏空 {budget.deficit}：停止该 scope 一切新授权"
        if budget.exhausted:
            note = "（本账期曾出现亏空）" if budget.ever_deficit else ""
            return f"失败预算已用尽{note}：新申请只能排队，不可开工"
        if budget.live_available <= 0:
            return "实时余量为零：新申请只能排队，不可开工"
        if budget.reserved_units:
            return (f"可批余量 {budget.live_available}，但 {budget.reserved_units} "
                    "个缺证预留可能在补证期满转成坏请求")
        return f"可批余量 {budget.live_available}"

    # ------------------------------------------------------------- 请求裁决
    def request_table(self) -> list[dict]:
        rows: list[dict] = []
        for request, intake in self.board.intakes.items():
            period = self.board.period_of[request]
            for service in intake.services:
                verdict = self.board.service_verdict(request, service)
                rows.append({
                    "request": request,
                    "period_start": period,
                    "scope": service,
                    "result": verdict.result.value,
                    "basis": verdict.basis.value,
                    "restated": verdict.restated,
                    "at": verdict.at,
                    "reason": verdict.reason,
                    "evidence": (f"{verdict.evidence[0]}:{verdict.evidence[1]}"
                                 if verdict.evidence else None),
                })
            edge = self.board.edge_verdict(request)
            rows.append({
                "request": request,
                "period_start": period,
                "scope": EDGE_SCOPE,
                "result": edge.result.value,
                "basis": edge.basis.value,
                "restated": edge.restated,
                "at": edge.at,
                "reason": edge.reason,
                "evidence": None,
            })
        return sorted(rows, key=lambda r: (r["period_start"], r["request"],
                                           r["scope"] == EDGE_SCOPE, r["scope"]))

    # ------------------------------------------------------------- 变更决策
    def change_table(self) -> list[dict]:
        rows = []
        for ticket in self.desk.tickets.values():
            risks = []
            deficit = self.ledger.has_deficit(ticket.scopes, ticket.period_start)
            if ticket.state is ChangeState.RUNNING and deficit:
                risks.append(
                    "执行中但 scope 出现亏空：授权保持有效，可继续/完成，"
                    "同 scope 新申请暂停")
            rows.append({
                "action": ticket.action_id,
                "period_start": ticket.period_start,
                "scopes": list(ticket.scopes),
                "units": ticket.units,
                "state": ticket.state.value,
                "updated_at": ticket.updated_at,
                "last_decision": (
                    ticket.history[-1] if ticket.state in
                    (ChangeState.FINISHED, ChangeState.WITHDRAWN)
                    else ticket.last_decision),
                "can_start_now": self._can_start_now(ticket),
                "risk": "；".join(risks) if risks else None,
                "authorization_count": len(ticket.snapshots),
            })
        return sorted(rows, key=lambda r: (r["period_start"], r["action"]))

    def _can_start_now(self, ticket) -> bool:
        if ticket.state not in (ChangeState.APPLIED,):
            return False
        action = self.desk._as_action(ticket, self.engine.now)
        ok, _ = self.ledger.can_authorize(action)
        return ok and not self.ledger.has_deficit(
            ticket.scopes, ticket.period_start)

    # ------------------------------------------------------------- 对账
    def reconciliation(self) -> list[ReconciliationItem]:
        """对比当时授权快照与当前重述，逐条解释差异来源（按变更×scope 去重）。"""
        items: list[ReconciliationItem] = []
        seen: set[tuple[str, str]] = set()
        for ticket in self.desk.tickets.values():
            if not ticket.snapshots:
                continue
            # 只取该变更最早一次授权快照作为“当时依据”，避免重复刷屏。
            snap = ticket.snapshots[0]
            for scope in snap.scopes:
                key = (ticket.action_id, scope)
                if key in seen:
                    continue
                seen.add(key)
                current = self.ledger.budget_for(snap.period_start, scope)
                then_occupied = snap.occupied[scope]
                if current.live_occupied == then_occupied:
                    continue
                cause = self._explain(scope, snap.period_start, snap.at)
                items.append(ReconciliationItem(
                    request=ticket.action_id, scope=scope,
                    period_start=snap.period_start,
                    authorized_as=(
                        f"第 {snap.at} 分钟授权时占用 {then_occupied}/"
                        f"{snap.budget[scope]}"),
                    restated_as=(
                        f"当前重述占用 {current.live_occupied}/"
                        f"{current.budget}（失败 {current.failure_units}，"
                        f"预留 {current.reserved_units}，"
                        f"变更 {current.change_committed}）"),
                    cause=cause,
                    evidence=None))
        # 请求级差异：重述过的裁决单列，说明是逾期补证、更正还是撤销
        for request, intake in self.board.intakes.items():
            period = self.board.period_of[request]
            for service in intake.services:
                verdict = self.board.service_verdict(request, service)
                if verdict.restated:
                    items.append(ReconciliationItem(
                        request=request, scope=service, period_start=period,
                        authorized_as="早期按当时证据（含暂结/预留）授权",
                        restated_as=(
                            f"当前重述为 {verdict.result.value}"
                            f"（{verdict.basis.value}）"),
                        cause=verdict.reason,
                        evidence=(f"{verdict.evidence[0]}:{verdict.evidence[1]}"
                                  if verdict.evidence else None)))
        return items

    def _explain(self, scope: str, period: int, since: int) -> str:
        related = [
            event for event in self.engine.log
            if event.at >= since and event.stream == "ledger"
            and (scope in event.delta_fail or scope in event.delta_hold)
        ]
        if not related:
            return "授权后无该 scope 的证据变动，差异来自其他变更占用/释放"
        # 直接累计授权后各次单位同步的净变化，fail<->hold 转换只反映在净效果里。
        net_fail = sum(e.delta_fail.get(scope, 0) for e in related)
        net_hold = sum(e.delta_hold.get(scope, 0) for e in related)
        return (f"授权后证据单位净变化：失败 {net_fail:+d}、预留 {net_hold:+d}"
            f"（首末含 fail↔hold 转换，仅看净效果），"
            f"首次第 {related[0].at} 分钟、末次第 {related[-1].at} 分钟")

    # ------------------------------------------------------------- 留痕
    def journal(self) -> list[dict]:
        return [event.as_dict() for event in self.engine.log]

    # ------------------------------------------------------------- 文本报表
    def render_text(self) -> str:
        lines: list[str] = []
        lines.append("== 预算结算与变更额度台（实时余量 / 当前重述 / 当时授权分开看） ==")
        lines.append("")
        lines.append("-- 预算台 --")
        header = ("账期", "scope", "预算", "重述失败", "缺证预留", "变更占用",
                  "实时余量", "亏空", "发版判断")
        lines.append(_row(header))
        for row in self.budget_table():
            lines.append(_row((
                row["period_start"], row["scope"], row["budget"],
                row["restated_failures"], row["reserved_holds"],
                row["change_committed"], row["live_available"], row["deficit"],
                row["decision"])))
        lines.append("")
        lines.append("-- 逻辑请求裁决（重试副本已合并，降级按承诺条款判定） --")
        lines.append(_row(("请求", "账期", "scope", "当前结果", "依据",
                           "重述", "时间", "说明")))
        for row in self.request_table():
            lines.append(_row((
                row["request"], row["period_start"], row["scope"], row["result"],
                row["basis"], "是" if row["restated"] else "", row["at"],
                row["reason"])))
        lines.append("")
        lines.append("-- 变更额度台 --")
        lines.append(_row(("变更", "账期", "scopes", "单位", "状态",
                           "现在能否开工", "最近决策/风险")))
        for row in self.change_table():
            lines.append(_row((
                row["action"], row["period_start"], ",".join(row["scopes"]),
                row["units"], row["state"],
                "是" if row["can_start_now"] else "否",
                (row["risk"] or row["last_decision"] or ""))))
        lines.append("")
        lines.append("-- 对账差异（当时授权 vs 当前重述） --")
        items = self.reconciliation()
        if not items:
            lines.append("无差异")
        for item in items:
            lines.append(
                f"* [{item.period_start}] {item.scope}"
                + (f" / {item.request}" if item.request != "-" else "")
                + f"：{item.authorized_as} -> {item.restated_as}；原因：{item.cause}")
        return "\n".join(lines)


def _row(values: tuple) -> str:
    return " | ".join(str(value) for value in values)
