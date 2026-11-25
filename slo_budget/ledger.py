"""错误预算台账。

三套数字严格分开，任何报表都不得把它们压成一个健康比例：

1. **实时可用余量**（live）：当前坏请求单位 + 缺证预留 + 已批准未开工/执行中
   变更占用后，现在还能批多少；
2. **当前重述的月度结算**（restated）：含逾期补证、更正、撤销后的最新裁决，
   未知在到期时按坏请求保守计入，绝不按零失败；
3. **当时授权依据**（authorizations）：每次批准/开工那一刻已有的证据快照，
   后续补证只改重述，不回改授权。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import ChangeAction, Objective


@dataclass(frozen=True)
class AuthorizationSnapshot:
    """一次授权在当时所依据的预算快照。"""

    action: str
    at: int
    units: int
    scopes: tuple[str, ...]
    period_start: int
    occupied: dict[str, int]
    budget: dict[str, int]
    basis: str  # apply / begin / finish 时刻的证据口径说明

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "at": self.at,
            "units": self.units,
            "scopes": list(self.scopes),
            "period_start": self.period_start,
            "occupied": dict(self.occupied),
            "budget": dict(self.budget),
            "basis": self.basis,
        }


@dataclass
class ScopeBudget:
    objective: Objective
    failure_units: int = 0           # 已确认/保守结算的坏请求单位
    reserved_units: int = 0          # 期限内的缺证预留单位
    change_holds: dict[str, int] = field(default_factory=dict)
    # action -> 授权快照；保留每次批准与开工的当时依据
    authorizations: list[AuthorizationSnapshot] = field(default_factory=list)
    # 单调递增的“证据单位净释放”序号：只有失败/预留单位真正减少才 +1，
    # fail<->hold 的内部转换抖动不推进，供回流门控判定。
    release_seq: int = 0
    last_evidence_release: int | None = None
    # 账期内是否曾出现失败单位超过预算的亏空；亏空不会因后续释放而“抹掉”。
    ever_deficit: bool = False

    @property
    def budget(self) -> int:
        return self.objective.error_units

    @property
    def change_committed(self) -> int:
        return sum(self.change_holds.values())

    @property
    def live_occupied(self) -> int:
        """实时占用：坏请求 + 缺证预留 + 变更占用（未使用也占余量）。"""
        return self.failure_units + self.reserved_units + self.change_committed

    @property
    def live_available(self) -> int:
        return self.budget - self.live_occupied

    @property
    def restated_used(self) -> int:
        """当前重述的月度结算用量（预留仍单列，不假装是零失败）。"""
        return self.failure_units

    @property
    def deficit(self) -> int:
        """当前亏空为正数；只按当前重述的失败单位判定，不把预留算作已发生失败。"""
        return max(0, self.failure_units - self.budget)

    @property
    def exhausted(self) -> bool:
        """失败单位已用尽预算（含超出），新授权无空间。"""
        return self.failure_units >= self.budget

    @property
    def healthy_ratio(self) -> float:
        """仅作参考的健康比例，不足以支撑发版决策。"""
        if self.budget == 0:
            return 1.0 if self.failure_units == 0 else 0.0
        return max(0.0, 1.0 - self.failure_units / self.budget)


class BudgetLedger:
    """按 (账期, scope) 维护预算占用与授权快照。"""

    def __init__(self) -> None:
        self.scopes: dict[tuple[int, str], ScopeBudget] = {}
        # 全局单调释放纪元：任一 scope 的证据单位净释放都使其 +1。
        self.release_epoch = 0

    def note_evidence_release(self) -> int:
        self.release_epoch += 1
        return self.release_epoch

    def add_objective(self, objective: Objective) -> None:
        key = (objective.month_start, objective.scope)
        if key in self.scopes:
            raise ValueError(f"预算重复定义：{objective.scope} @ {objective.month_start}")
        self.scopes[key] = ScopeBudget(objective=objective)

    def budget_for(self, period: int, scope: str) -> ScopeBudget:
        try:
            return self.scopes[(period, scope)]
        except KeyError:
            raise ValueError(f"账期 {period} 的 scope {scope} 没有已发布预算") from None

    def periods(self) -> list[int]:
        return sorted({period for period, _ in self.scopes})

    # ------------------------------------------------------------- 证据入账
    def apply_evidence_delta(self, period: int, scope: str,
                             delta_fail: int, delta_hold: int) -> None:
        budget = self.budget_for(period, scope)
        budget.failure_units = max(0, budget.failure_units + delta_fail)
        budget.reserved_units = max(0, budget.reserved_units + delta_hold)

    # ------------------------------------------------------------- 变更占用
    def can_authorize(self, action: ChangeAction) -> tuple[bool, dict[str, int]]:
        """所有涉及 scope（含共享 edge）都有实时余量才可批准。"""
        shortfall: dict[str, int] = {}
        for scope in action.scopes:
            budget = self.budget_for(action.period_start, scope)
            if budget.live_available < action.units:
                shortfall[scope] = action.units - budget.live_available
        return not shortfall, shortfall

    def hold_change(self, action: ChangeAction) -> None:
        for scope in action.scopes:
            budget = self.budget_for(action.period_start, scope)
            budget.change_holds[action.action] = (
                budget.change_holds.get(action.action, 0) + action.units)

    def release_change(self, action: ChangeAction) -> None:
        for scope in action.scopes:
            budget = self.budget_for(action.period_start, scope)
            held = budget.change_holds.pop(action.action, 0)
            if held != action.units:
                raise ValueError(
                    f"变更 {action.action} 在 {scope} 占用 {held}，与申请 {action.units} 不符")

    def snapshot(self, action: ChangeAction, basis: str) -> AuthorizationSnapshot:
        occupied = {
            scope: self.budget_for(action.period_start, scope).live_occupied
            for scope in action.scopes
        }
        budget = {
            scope: self.budget_for(action.period_start, scope).budget
            for scope in action.scopes
        }
        snap = AuthorizationSnapshot(
            action=action.action, at=action.at, units=action.units,
            scopes=action.scopes, period_start=action.period_start,
            occupied=occupied, budget=budget, basis=basis)
        for scope in action.scopes:
            self.budget_for(action.period_start, scope).authorizations.append(snap)
        return snap

    def has_deficit(self, scopes: tuple[str, ...], period: int) -> dict[str, int]:
        return {
            scope: self.budget_for(period, scope).deficit
            for scope in scopes
            if self.budget_for(period, scope).deficit > 0
        }

    # ------------------------------------------------------------- 视图
    def live_view(self) -> dict[tuple[int, str], dict]:
        return {
            key: {
                "budget": b.budget,
                "failure_units": b.failure_units,
                "reserved_units": b.reserved_units,
                "change_committed": b.change_committed,
                "live_available": b.live_available,
                "deficit": b.deficit,
            }
            for key, b in self.scopes.items()
        }

    def restated_view(self) -> dict[tuple[int, str], dict]:
        return {
            key: {
                "budget": b.budget,
                "restated_failures": b.failure_units,
                "reserved_units": b.reserved_units,
                "deficit": b.deficit,
                "healthy_ratio": round(b.healthy_ratio, 4),
            }
            for key, b in self.scopes.items()
        }
