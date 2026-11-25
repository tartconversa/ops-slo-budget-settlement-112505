"""证据裁决。

对每个「逻辑请求 × 涉及服务」维护一条状态机，对每个逻辑请求维护一条
共享 edge 状态。核心口径：

* 逻辑请求以 intake id 为准，重试只是同一请求下的多份边缘尝试；
* 有效客户端终态（未撤销的 received/failed）能证明用户可见结果，
  与边缘证据冲突时以客户端为准；撤销使同 ref 旧事实失效，失效后回到边缘口径；
* 有客户端收件渠道但终态迟到时，补证期内先保守预留一个单位；
  边缘有明确终态可先暂结（预期结果先定），预留与缺证风险留存到补证期满；
* 补证期满仍无任何可裁决终态，按坏请求保守结算并标为未知，绝不按零失败处理；
* 逾期到达的客户端补证/更正/撤销进入当前重述，不改变当时已作出的授权；
* 共享 edge 对同一逻辑请求最多计一个单位，任一下游未达成即计一次。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    CLIENT_SOURCE,
    EDGE_SCOPE,
    Basis,
    Fact,
    Intake,
    Objective,
    ServiceCase,
    UserResult,
)


@dataclass(frozen=True)
class Verdict:
    """某个请求-服务（或 edge 请求）在某一时刻的裁决。"""

    result: UserResult
    basis: Basis
    reason: str
    at: int
    evidence: tuple[str, str] | None = None
    restated: bool = False

    @property
    def failure_unit(self) -> bool:
        """当前是否占用一个坏请求单位（含保守结算的未知）。"""
        return self.result in (UserResult.FAILED, UserResult.UNKNOWN_FAILED)

    @property
    def reserved_unit(self) -> bool:
        """期限内的保守预留单位（与坏请求单位互斥）。"""
        return self.result is UserResult.HELD


@dataclass(frozen=True)
class JournalEvent:
    """台账留痕：解释每一单位变动与每次授权的依据。"""

    at: int
    kind: str
    request: str | None
    scope: str | None
    period_start: int | None
    detail: str
    delta_fail: int = 0
    delta_hold: int = 0
    evidence: tuple[str, str] | None = None
    action: str | None = None


@dataclass
class _ServiceState:
    intake: Intake
    service: str
    objective: Objective
    case: ServiceCase
    verdict: Verdict
    deadline: int

    @property
    def channel_open(self) -> bool:
        return self.intake.client_receipt


class EvidenceBoard:
    """增量证据板：登记事实、推进补证期限、产出裁决与留痕事件。"""

    def __init__(self) -> None:
        self.objectives: dict[tuple[int, str], Objective] = {}
        self.intakes: dict[str, Intake] = {}
        self.period_of: dict[str, int] = {}
        self.cases: dict[tuple[str, str], _ServiceState] = {}
        self.edge_verdicts: dict[str, Verdict] = {}
        self.journal: list[JournalEvent] = []
        self._fact_index: dict[tuple[str, str, str], Fact] = {}

    # ------------------------------------------------------------------ 配置
    def register_objective(self, objective: Objective) -> None:
        key = (objective.month_start, objective.scope)
        if key in self.objectives:
            raise ValueError(f"承诺重复：{objective.scope} @ {objective.month_start}")
        self.objectives[key] = objective

    def period_for_minute(self, minute: int) -> int:
        for start, end in {(p[0], self.objectives[p].month_end)
                           for p in self.objectives}:
            if start <= minute < end:
                return start
        raise ValueError(f"分钟 {minute} 不属于任何已发布承诺账期")

    def objective_for(self, request: str, scope: str) -> Objective:
        return self.objectives[(self.period_of[request], scope)]

    # ----------------------------------------------------------------- 接纳
    def register_intake(self, intake: Intake) -> list[JournalEvent]:
        if intake.request in self.intakes:
            raise ValueError(f"逻辑请求重复接纳：{intake.request}")
        period = self.period_for_minute(intake.minute)
        self.period_of[intake.request] = period
        self.intakes[intake.request] = intake
        events: list[JournalEvent] = []
        for service in intake.services:
            if (period, service) not in self.objectives:
                raise ValueError(
                    f"请求 {intake.request} 的服务 {service} 在账期 {period} 无承诺")
            objective = self.objectives[(period, service)]
            deadline = intake.minute + objective.holds_until_min
            case = ServiceCase(request=intake.request, service=service)
            if intake.client_receipt:
                verdict = Verdict(
                    UserResult.HELD, Basis.HOLD,
                    "客户端收件渠道存在，终态到达前保守预留一个缺证单位",
                    intake.minute)
            else:
                verdict = Verdict(
                    UserResult.OPEN, Basis.HOLD,
                    "无客户端收件渠道，零占用等待可归属的边缘完成证据",
                    intake.minute)
            state = _ServiceState(intake, service, objective, case, verdict, deadline)
            self.cases[(intake.request, service)] = state
            events.append(JournalEvent(
                at=intake.minute, kind="intake", request=intake.request,
                scope=service, period_start=period,
                detail=(f"逻辑请求 {intake.request} 接纳，涉及 {service}；"
                        + ("有客户端收件渠道，" if intake.client_receipt
                           else "无客户端收件渠道，")
                        + f"补证期限至第 {deadline} 分钟"),
                delta_hold=1 if intake.client_receipt else 0))
        # edge 初始状态：有渠道先预留一次，无渠道零占用等待边缘
        edge_hold = intake.client_receipt
        self.edge_verdicts[intake.request] = Verdict(
            UserResult.HELD if edge_hold else UserResult.OPEN,
            Basis.HOLD,
            "共享 edge 随逻辑请求建立，任一涉及服务未决即按一次预留",
            intake.minute)
        if edge_hold:
            events.append(JournalEvent(
                at=intake.minute, kind="edge-hold", request=intake.request,
                scope=EDGE_SCOPE, period_start=period,
                detail=f"edge 为请求 {intake.request} 保守预留一个单位",
                delta_hold=1))
        return events

    # ----------------------------------------------------------------- 事实
    def register_fact(self, fact: Fact) -> list[JournalEvent]:
        intake = self.intakes.get(fact.request)
        if intake is None:
            raise ValueError(f"事实 {fact.ref} 引用了未知逻辑请求 {fact.request}")
        if fact.service not in intake.services:
            raise ValueError(
                f"事实 {fact.ref} 的服务 {fact.service} 不在请求 {fact.request} 的承接范围")
        if fact.at < intake.minute:
            raise ValueError(f"事实 {fact.ref} 早于请求 {fact.request} 的接纳时间")
        state = self.cases[(fact.request, fact.service)]
        self._store_revision(state, fact)
        events: list[JournalEvent] = []
        events.extend(self._adjudicate(state, fact.at))
        events.extend(self._recompute_edge(fact.request, fact.at))
        return events

    def _store_revision(self, state: _ServiceState, fact: Fact) -> None:
        refs = (state.case.client_refs if fact.source == CLIENT_SOURCE
                else state.case.edge_refs)
        prior = refs.get(fact.ref)
        if prior is not None and fact.revision <= prior.revision:
            raise ValueError(
                f"事实 {fact.ref} revision 倒退：{fact.revision} <= {prior.revision}")
        refs[fact.ref] = fact

    # ------------------------------------------------------------- 期限推进
    def advance(self, now: int) -> list[JournalEvent]:
        """把所有补证期限不晚于 ``now`` 的请求推到到期结算。"""
        events: list[JournalEvent] = []
        for request, intake in self.intakes.items():
            for service in intake.services:
                state = self.cases[(request, service)]
                if state.verdict.result in (UserResult.FAILED,
                                            UserResult.SATISFIED,
                                            UserResult.UNKNOWN_FAILED):
                    continue
                if now >= state.deadline:
                    events.extend(self._settle_at_deadline(state))
            events.extend(self._recompute_edge(request, now))
        return events

    def _settle_at_deadline(self, state: _ServiceState) -> list[JournalEvent]:
        terminal = state.case.client_terminal()
        if terminal is not None:
            return self._adjudicate(state, state.deadline)
        deliverable = state.case.edge_deliverable()
        request, service = state.intake.request, state.service
        if deliverable is None:
            verdict = Verdict(
                UserResult.UNKNOWN_FAILED, Basis.CONSERVATIVE,
                f"补证期满（第 {state.deadline} 分钟）仍无可裁决终态，"
                "未知按坏请求保守结算，不得按零失败处理",
                state.deadline)
            return self._transition(state, verdict, state.deadline,
                                    delta_fail=+1,
                                    delta_hold=-1 if state.verdict.reserved_unit else 0,
                                    kind="unknown-settle")
        met = self._edge_promise(state, deliverable)
        verdict = Verdict(
            UserResult.SATISFIED if met else UserResult.FAILED,
            Basis.EDGE,
            f"补证期满无有效客户端终态，按可归属边缘完成证据结算"
            f"（{deliverable.ref}，{self._promise_text(state, deliverable)}）",
            state.deadline,
            evidence=(deliverable.source, deliverable.ref))
        return self._transition(
            state, verdict, state.deadline,
            delta_fail=0 if met else +1,
            delta_hold=-1 if state.verdict.reserved_unit else 0,
            kind="edge-settle")

    # ------------------------------------------------------------- 裁决逻辑
    def _adjudicate(self, state: _ServiceState, now: int) -> list[JournalEvent]:
        terminal = state.case.client_terminal()
        if terminal is not None:
            return self._client_verdict(state, terminal, now)
        deliverable = state.case.edge_deliverable()
        if deliverable is None:
            return self._stay_held(state, now, "暂无可归属的边缘完成终态")
        if not state.channel_open:
            # 无客户端收件渠道：不存在可补证的客户端证据，边缘明确终态即终局，
            # 直接按服务承诺结算，不预留、不等待补证期。
            return self._edge_only_settle(state, deliverable, now)
        return self._provisional(state, deliverable, now)

    def _edge_only_settle(self, state: _ServiceState, fact: Fact,
                          now: int) -> list[JournalEvent]:
        met = self._edge_promise(state, fact)
        verdict = Verdict(
            UserResult.SATISFIED if met else UserResult.FAILED,
            Basis.EDGE,
            f"无客户端收件渠道，按可归属边缘完成证据 {fact.ref} 终局结算；"
            + self._promise_text(state, fact),
            now, evidence=(fact.source, fact.ref))
        delta_fail = (1 if verdict.failure_unit else 0) - (
            1 if state.verdict.failure_unit else 0)
        delta_hold = (1 if verdict.reserved_unit else 0) - (
            1 if state.verdict.reserved_unit else 0)
        return self._transition(state, verdict, now,
                                delta_fail=delta_fail, delta_hold=delta_hold,
                                kind="edge-only-settle")

    def _client_verdict(self, state: _ServiceState, fact: Fact,
                        now: int) -> list[JournalEvent]:
        met = state.objective.promise_met(
            fact.outcome == "received", fact.latency_ms, fact.fallback)
        late = now > state.deadline or state.verdict.restated
        basis = Basis.RESTATED if late else Basis.CLIENT
        reason = (
            f"有效客户端记录 {fact.ref} 证明用户可见结果为 {fact.outcome}"
            + ("（逾期补证，记入当前重述）" if late else "")
            + "，双边冲突时以客户端为准；"
            + self._promise_text(state, fact))
        verdict = Verdict(
            UserResult.SATISFIED if met else UserResult.FAILED,
            basis, reason, now, evidence=(fact.source, fact.ref),
            restated=late or state.verdict.restated)
        delta_fail = (1 if verdict.failure_unit else 0) - (
            1 if state.verdict.failure_unit else 0)
        delta_hold = (1 if verdict.reserved_unit else 0) - (
            1 if state.verdict.reserved_unit else 0)
        kind = "client-restate" if late else "client-settle"
        return self._transition(state, verdict, now,
                                delta_fail=delta_fail, delta_hold=delta_hold,
                                kind=kind)

    def _provisional(self, state: _ServiceState, fact: Fact,
                     now: int) -> list[JournalEvent]:
        """边缘明确终态的暂结：预期结果先定，缺证风险与预留保留到补证期满。

        仅用于有客户端收件渠道的请求；无渠道请求走 :meth:`_edge_only_settle`
        直接终局结算。
        """
        met = self._edge_promise(state, fact)
        if state.verdict.basis is Basis.PROVISIONAL and (
                state.verdict.evidence == (fact.source, fact.ref)):
            return []
        verdict = Verdict(
            UserResult.HELD, Basis.PROVISIONAL,
            f"边缘终态 {fact.ref} 暂结为{'达标' if met else '未达标'}，"
            "客户端缺证风险保留至补证期满，预留单位不释放",
            now, evidence=(fact.source, fact.ref))
        # 预留单位数量不变（仍是一个 hold），只换依据，因此不产生单位变动。
        return self._transition(state, verdict, now, kind="provisional")

    def _stay_held(self, state: _ServiceState, now: int, reason: str
                   ) -> list[JournalEvent]:
        if state.verdict.result in (UserResult.HELD, UserResult.OPEN):
            return []
        verdict = Verdict(UserResult.HELD, Basis.HOLD, reason, now)
        return self._transition(state, verdict, now, kind="hold-reassert")

    def _edge_promise(self, state: _ServiceState, fact: Fact) -> bool:
        return state.objective.promise_met(
            fact.outcome == "received", fact.latency_ms, fact.fallback)

    def _promise_text(self, state: _ServiceState, fact: Fact) -> str:
        objective = state.objective
        parts = [f"结果={fact.outcome}"]
        if fact.latency_ms is not None and objective.latency_ms is not None:
            parts.append(
                f"时延 {fact.latency_ms}ms 对承诺 {objective.latency_ms}ms")
        if objective.fallback_ok is not None and fact.fallback is not None:
            parts.append(
                f"降级={fact.fallback}，承诺允许降级={objective.fallback_ok}")
        return "，".join(parts)

    # ------------------------------------------------------------- 状态切换
    def _transition(self, state: _ServiceState, verdict: Verdict, now: int,
                    delta_fail: int = 0, delta_hold: int = 0,
                    kind: str = "") -> list[JournalEvent]:
        previous = state.verdict
        if previous == verdict:
            return []
        state.verdict = verdict
        period = self.period_of[state.intake.request]
        event = JournalEvent(
            at=now, kind=kind, request=state.intake.request,
            scope=state.service, period_start=period,
            detail=(f"{state.service}：{previous.result.value}({previous.basis.value}) "
                    f"→ {verdict.result.value}({verdict.basis.value})；{verdict.reason}"),
            delta_fail=delta_fail, delta_hold=delta_hold,
            evidence=verdict.evidence)
        return [event]

    # ------------------------------------------------------------- edge 汇总
    def _recompute_edge(self, request: str, now: int) -> list[JournalEvent]:
        intake = self.intakes[request]
        service_verdicts = [self.cases[(request, s)].verdict for s in intake.services]
        any_fail = any(v.failure_unit for v in service_verdicts)
        any_held = any(v.reserved_unit or v.basis is Basis.PROVISIONAL
                       for v in service_verdicts)
        any_open = any(v.result is UserResult.OPEN for v in service_verdicts)
        restated = any(v.restated for v in service_verdicts)
        previous = self.edge_verdicts[request]
        if any_fail:
            failing_verdicts = [
                self.cases[(request, s)].verdict
                for s in intake.services
                if self.cases[(request, s)].verdict.failure_unit]
            # edge 依据从失败服务的裁决依据派生：任一来自客户端（含重述）即客户端，
            # 全部来自边缘保守/终态结算则记边缘，避免把无渠道失败误标成客户端。
            if restated or any(v.basis in (Basis.CLIENT, Basis.RESTATED)
                               for v in failing_verdicts):
                basis = Basis.RESTATED if restated else Basis.CLIENT
            else:
                basis = Basis.EDGE
            failing = [s for s in intake.services
                       if self.cases[(request, s)].verdict.failure_unit]
            verdict = Verdict(
                UserResult.FAILED, basis,
                f"共享 edge 按逻辑请求只计一次；未达成服务：{', '.join(failing)}",
                now, restated=restated)
        elif any_held:
            provisional = all(v.basis is Basis.PROVISIONAL
                              for v in service_verdicts)
            verdict = Verdict(
                UserResult.HELD,
                Basis.PROVISIONAL if provisional else Basis.HOLD,
                "edge 等待涉及服务的客户端补证，继续按一次预留",
                now, restated=restated)
        elif any_open:
            verdict = Verdict(
                UserResult.OPEN, Basis.HOLD,
                "无客户端渠道且边缘终态未齐，edge 零占用等待证据",
                now)
        else:
            verdict = Verdict(
                UserResult.SATISFIED,
                Basis.RESTATED if restated else Basis.EDGE,
                "全部涉及服务达成承诺，edge 不计失败单位",
                now, restated=restated)
        if verdict == previous:
            return []
        delta_fail = (1 if verdict.failure_unit else 0) - (
            1 if previous.failure_unit else 0)
        delta_hold = (1 if verdict.reserved_unit else 0) - (
            1 if previous.reserved_unit else 0)
        self.edge_verdicts[request] = verdict
        return [JournalEvent(
            at=now, kind="edge-transition", request=request, scope=EDGE_SCOPE,
            period_start=self.period_of[request],
            detail=(f"edge：{previous.result.value}({previous.basis.value}) "
                    f"→ {verdict.result.value}({verdict.basis.value})；{verdict.reason}"),
            delta_fail=delta_fail, delta_hold=delta_hold)]

    # ----------------------------------------------------------------- 查询
    def service_verdict(self, request: str, service: str) -> Verdict:
        return self.cases[(request, service)].verdict

    def edge_verdict(self, request: str) -> Verdict:
        return self.edge_verdicts[request]

    def request_cases(self, request: str) -> list[_ServiceState]:
        return [self.cases[(request, s)] for s in self.intakes[request].services]

    def current_units(self, period: int, scope: str) -> tuple[int, int]:
        """返回 (坏请求单位, 预留单位)。edge 按请求去重后计数。"""
        fail = hold = 0
        if scope == EDGE_SCOPE:
            for request, period_start in self.period_of.items():
                if period_start != period:
                    continue
                verdict = self.edge_verdicts[request]
                fail += int(verdict.failure_unit)
                hold += int(verdict.reserved_unit)
            return fail, hold
        for (request, service), state in self.cases.items():
            if self.period_of[request] != period or service != scope:
                continue
            fail += int(state.verdict.failure_unit)
            hold += int(state.verdict.reserved_unit)
        return fail, hold
