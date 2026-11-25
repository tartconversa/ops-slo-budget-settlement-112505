"""不可变领域值对象与资料行校验。

时间统一为演练起点后的分钟整数。每条事实由 ``(source, ref)`` 唯一标识，
``revision`` 只在同一来源标识内递增；撤销行使同一 ref 的旧事实失效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

EDGE_SCOPE = "edge"
CLIENT_SOURCE = "client"
EDGE_SOURCE = "edge"

OUTCOMES = ("received", "failed", "unknown", "withdrawn")
CHANGE_KINDS = ("apply", "begin", "finish", "withdraw")


class UserResult(str, Enum):
    """用户对某服务部分实际收到的结果。"""

    OPEN = "open"                    # 无客户端渠道且尚无边缘终态，零占用等待证据
    SATISFIED = "satisfied"          # 达成承诺（允许的降级也落在承诺内）
    FAILED = "failed"                # 有有效证据证明未达成承诺
    HELD = "held"                    # 补证期内保守预留，尚未裁决
    UNKNOWN_FAILED = "unknown_failed"  # 补证期满仍无法裁决，按坏请求保守结算


class Basis(str, Enum):
    """一次裁决所依据的证据来源。"""

    CLIENT = "client"                # 能证明用户可见结果的有效客户端记录
    EDGE = "edge"                    # 可归属的边缘完成证据（缺客户端证据时）
    PROVISIONAL = "provisional"      # 承诺窗口内边缘明确终态的暂结
    HOLD = "hold"                    # 期限内缺证预留，没有终态
    CONSERVATIVE = "conservative"    # 补证期满的未知保守结算
    RESTATED = "restated"            # 逾期补证/更正触发的当前重述


class ChangeState(str, Enum):
    APPLIED = "applied"              # 已批准未开工，占用实时余量、尚可回流
    PENDING = "pending"              # 争用排队，等待余量按到达顺序重审
    RUNNING = "running"              # 已开工，授权依据固定，不再回流
    FINISHED = "finished"            # 已完成，保留原授权依据
    WITHDRAWN = "withdrawn"          # 撤销，释放未开工占用


@dataclass(frozen=True)
class Objective:
    """一条发布的服务承诺。条款按账期版本化，不回溯已接纳请求。"""

    scope: str
    month_start: int
    month_end: int
    error_units: int
    fallback_ok: bool | None
    latency_ms: int | None
    holds_until_min: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Objective":
        required = ("scope", "month_start", "month_end", "error_units",
                    "holds_until_min")
        _require_fields(row, required, "objectives")
        if int(row["month_end"]) <= int(row["month_start"]):
            raise ValueError("objectives：month_end 必须晚于 month_start")
        if int(row["error_units"]) < 0 or int(row["holds_until_min"]) < 0:
            raise ValueError("objectives：error_units 与 holds_until_min 不能为负")
        return cls(
            scope=str(row["scope"]),
            month_start=int(row["month_start"]),
            month_end=int(row["month_end"]),
            error_units=int(row["error_units"]),
            fallback_ok=_optional_bool(row.get("fallback_ok"), "fallback_ok"),
            latency_ms=_optional_int(row.get("latency_ms"), "latency_ms"),
            holds_until_min=int(row["holds_until_min"]),
        )

    @property
    def is_edge(self) -> bool:
        return self.scope == EDGE_SCOPE

    def promise_met(self, received: bool, latency_ms: int | None,
                    fallback: bool | None) -> bool:
        """按该服务承诺判定一次用户级响应是否达标。

        * ``received=False`` 一定不达标；
        * 服务不允许降级（``fallback_ok=False``）时降级版本不达标；
        * 超过承诺时延不达标。边缘共享承诺没有这两项字段，不参与本判定。
        """
        if not received:
            return False
        if self.fallback_ok is False and fallback:
            return False
        if (self.latency_ms is not None and latency_ms is not None
                and latency_ms > self.latency_ms):
            return False
        return True


@dataclass(frozen=True)
class Intake:
    """边缘首次承接的逻辑请求；重试不另立逻辑请求。"""

    minute: int
    request: str
    services: tuple[str, ...]
    client_receipt: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Intake":
        _require_fields(row, ("minute", "request", "services", "client_receipt"),
                        "intake")
        services = tuple(row["services"])
        if not services:
            raise ValueError(f"intake {row.get('request')}：services 不能为空")
        return cls(
            minute=int(row["minute"]),
            request=str(row["request"]),
            services=services,
            client_receipt=bool(row["client_receipt"]),
        )

    @property
    def period_start(self) -> int:
        # 由 engine 按承诺账期映射，这里只保留原始进入分钟。
        return self.minute


@dataclass(frozen=True)
class Fact:
    """一条响应证据或来源更正。"""

    at: int
    request: str
    service: str
    source: str
    ref: str
    revision: int
    outcome: str
    latency_ms: int | None
    fallback: bool | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Fact":
        _require_fields(
            row,
            ("at", "request", "service", "source", "ref", "revision", "outcome"),
            "facts",
        )
        if row["outcome"] not in OUTCOMES:
            raise ValueError(f"facts：未知 outcome {row['outcome']!r}")
        if row["source"] not in (CLIENT_SOURCE, EDGE_SOURCE):
            raise ValueError(f"facts：source 只能是 client/edge，得到 {row['source']!r}")
        return cls(
            at=int(row["at"]),
            request=str(row["request"]),
            service=str(row["service"]),
            source=str(row["source"]),
            ref=str(row["ref"]),
            revision=int(row["revision"]),
            outcome=str(row["outcome"]),
            latency_ms=_optional_int(row.get("latency_ms"), "latency_ms"),
            fallback=_optional_bool(row.get("fallback"), "fallback"),
        )

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.ref

    @property
    def is_withdrawal(self) -> bool:
        return self.outcome == "withdrawn"

    @property
    def is_client_terminal(self) -> bool:
        """有效客户端终态：received/failed；unknown 与 withdrawn 不是终态。"""
        return self.source == CLIENT_SOURCE and self.outcome in ("received", "failed")


@dataclass(frozen=True)
class ChangeAction:
    """变更经理提交的一个动作。同一 action 标识同一条变更。"""

    at: int
    action: str
    kind: str
    period_start: int
    scopes: tuple[str, ...]
    units: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ChangeAction":
        _require_fields(
            row, ("at", "action", "kind", "period_start", "scopes", "units"),
            "changes",
        )
        if row["kind"] not in CHANGE_KINDS:
            raise ValueError(f"changes：未知 kind {row['kind']!r}")
        scopes = tuple(row["scopes"])
        if not scopes:
            raise ValueError(f"changes {row.get('action')}：scopes 不能为空")
        if int(row["units"]) <= 0:
            raise ValueError(f"changes {row.get('action')}：units 必须为正数")
        return cls(
            at=int(row["at"]),
            action=str(row["action"]),
            kind=str(row["kind"]),
            period_start=int(row["period_start"]),
            scopes=scopes,
            units=int(row["units"]),
        )


@dataclass
class ServiceCase:
    """一个逻辑请求 × 一个涉及服务的证据容器。"""

    request: str
    service: str
    client_refs: dict[str, Fact] = field(default_factory=dict)
    edge_refs: dict[str, Fact] = field(default_factory=dict)

    def effective_facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for refs in (self.client_refs, self.edge_refs):
            for fact in refs.values():
                if fact.outcome != "withdrawn":
                    facts.append(fact)
        return facts

    def client_terminal(self) -> Fact | None:
        """当前有效的客户端终态（revision 最高、未撤销）。"""
        candidates = [f for f in self.client_refs.values() if f.is_client_terminal]
        if not candidates:
            return None
        return max(candidates, key=lambda f: (f.revision, f.at))

    def edge_deliverable(self) -> Fact | None:
        """边缘侧可归属的明确完成终态。

        多次边缘尝试以最终可交付结果为准：只要任一尝试 received，
        已成功的可交付结果就不能被另一次独立超时替代。
        """
        effective = [f for f in self.edge_refs.values()
                     if f.outcome in ("received", "failed")]
        received = [f for f in effective if f.outcome == "received"]
        if received:
            return max(received, key=lambda f: (f.revision, f.at))
        if effective:
            return max(effective, key=lambda f: (f.revision, f.at))
        return None


def _require_fields(row: dict[str, Any], fields: tuple[str, ...], feed: str) -> None:
    missing = [name for name in fields if name not in row]
    if missing:
        raise ValueError(f"{feed}：缺少字段 {', '.join(missing)}（记录={row!r}）")


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"字段 {name} 必须为布尔或 null，得到 {value!r}")
    return value
