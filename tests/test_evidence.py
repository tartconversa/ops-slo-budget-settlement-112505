"""证据裁决口径测试：重试副本、降级、补证期限、未知保守结算、双边冲突。"""
import unittest

from slo_budget.model import (
    Basis,
    ChangeAction,
    Fact,
    Intake,
    Objective,
    UserResult,
)
from slo_budget.engine import SettlementEngine


def objective(scope, start=0, end=100, units=5, fallback_ok=False,
              latency=100, holds=10):
    return Objective(scope, start, end, units, fallback_ok, latency, holds)


class EngineBuildMixin:
    def build(self, objectives, intakes, facts=(), changes=()):
        engine = SettlementEngine()
        for obj in objectives:
            engine.register_objective(obj)
        for intake in intakes:
            engine.register_intake(intake)
        for fact in facts:
            engine.register_fact(fact)
        for action in changes:
            engine.register_change(action)
        return engine


class RetryAndFallbackTest(EngineBuildMixin, unittest.TestCase):
    def _objs(self):
        return [
            objective("edge"),
            Objective("svc", 0, 100, 5, fallback_ok=True, latency_ms=180,
                      holds_until_min=10),
        ]

    def test_retry_duplicate_edge_attempts_merge_to_one_logical_request(self):
        # 第一次边缘超时失败、重试到另一机房成功：只认一个逻辑请求。
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [
            Fact(11, "r1", "svc", "edge", "e1", 1, "failed", None, False),
            Fact(12, "r1", "svc", "edge", "e2", 1, "received", 120, False),
        ]
        engine = self.build(self._objs(), intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.HELD)  # 有客户端渠道，暂结仍预留
        self.assertIs(verdict.basis, Basis.PROVISIONAL)
        self.assertEqual(verdict.evidence, ("edge", "e2"))

    def test_allowed_fallback_satisfies_promise(self):
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [Fact(11, "r1", "svc", "client", "c1", 1, "received", 150, True)]
        engine = self.build(self._objs(), intakes, facts)
        self.assertIs(
            engine.board.service_verdict("r1", "svc").result,
            UserResult.SATISFIED)

    def test_disallowed_fallback_is_failure_even_with_http_success(self):
        objs = [objective("edge"), objective("svc", fallback_ok=False, latency=200)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [Fact(11, "r1", "svc", "client", "c1", 1, "received", 150, True)]
        engine = self.build(objs, intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.FAILED)

    def test_latency_breach_is_failure(self):
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [Fact(11, "r1", "svc", "client", "c1", 1, "received", 250, False)]
        engine = self.build(self._objs(), intakes, facts)
        self.assertIs(
            engine.board.service_verdict("r1", "svc").result, UserResult.FAILED)


class ClientEdgeConflictTest(EngineBuildMixin, unittest.TestCase):
    def test_client_record_overrides_edge_when_conflicting(self):
        objs = [objective("edge"), objective("svc", fallback_ok=True, latency=200)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [
            Fact(11, "r1", "svc", "edge", "e1", 1, "received", 120, False),
            Fact(12, "r1", "svc", "client", "c1", 1, "failed", None, False),
        ]
        engine = self.build(objs, intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.FAILED)
        self.assertIs(verdict.basis, Basis.CLIENT)

    def test_withdrawal_invalidates_client_fact_and_returns_to_edge(self):
        objs = [objective("edge"), objective("svc", fallback_ok=True, latency=200)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [
            Fact(11, "r1", "svc", "edge", "e1", 1, "received", 120, False),
            Fact(12, "r1", "svc", "client", "c1", 1, "failed", None, False),
            Fact(13, "r1", "svc", "client", "c1", 2, "withdrawn", None, False),
        ]
        engine = self.build(objs, intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        # 撤销后无有效客户端终态，回到边缘暂结口径，仍在补证期内预留。
        self.assertIs(verdict.result, UserResult.HELD)
        self.assertIs(verdict.basis, Basis.PROVISIONAL)


class HoldsAndUnknownTest(EngineBuildMixin, unittest.TestCase):
    def test_held_reservation_is_not_zero_failure(self):
        objs = [objective("edge"), objective("svc", holds=10)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        engine = self.build(objs, intakes)
        fail, hold = engine.board.current_units(0, "svc")
        self.assertEqual((fail, hold), (0, 1))

    def test_unknown_at_deadline_settles_conservatively_as_failure(self):
        objs = [objective("edge"), objective("svc", holds=5)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        engine = self.build(objs, intakes)
        engine.advance_to(15)  # 补证期满，无任何终态
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.UNKNOWN_FAILED)
        self.assertIs(verdict.basis, Basis.CONSERVATIVE)
        fail, hold = engine.board.current_units(0, "svc")
        self.assertEqual((fail, hold), (1, 0))

    def test_late_client_evidence_after_deadline_is_restatement(self):
        objs = [objective("edge"), objective("svc", holds=5, fallback_ok=True,
                                             latency=200)]
        intakes = [Intake(10, "r1", ("svc",), True)]
        facts = [Fact(20, "r1", "svc", "client", "c1", 1, "received", 100, False)]
        engine = self.build(objs, intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.SATISFIED)
        self.assertIs(verdict.basis, Basis.RESTATED)
        self.assertTrue(verdict.restated)

    def test_no_client_channel_waits_open_without_reservation(self):
        objs = [objective("edge"), objective("svc", holds=10)]
        intakes = [Intake(10, "r1", ("svc",), False)]
        engine = self.build(objs, intakes)
        self.assertIs(
            engine.board.service_verdict("r1", "svc").result, UserResult.OPEN)
        self.assertEqual(engine.board.current_units(0, "svc"), (0, 0))

    def test_no_client_channel_edge_terminal_settles_immediately(self):
        # 无客户端渠道不存在补证可能：边缘明确终态即终局，不预留、不等补证期。
        objs = [objective("edge"), objective("svc", holds=10,
                                             fallback_ok=True, latency=200)]
        intakes = [Intake(10, "r1", ("svc",), False)]
        facts = [Fact(11, "r1", "svc", "edge", "e1", 1, "received", 120, False)]
        engine = self.build(objs, intakes, facts)
        verdict = engine.board.service_verdict("r1", "svc")
        self.assertIs(verdict.result, UserResult.SATISFIED)
        self.assertIs(verdict.basis, Basis.EDGE)
        self.assertEqual(engine.board.current_units(0, "svc"), (0, 0))
        self.assertEqual(engine.board.current_units(0, "edge"), (0, 0))

    def test_no_client_channel_edge_failure_is_not_labeled_client(self):
        objs = [objective("edge"), objective("svc", holds=10)]
        intakes = [Intake(10, "r1", ("svc",), False)]
        facts = [Fact(11, "r1", "svc", "edge", "e1", 1, "failed", None, False)]
        engine = self.build(objs, intakes, facts)
        self.assertIs(
            engine.board.service_verdict("r1", "svc").basis, Basis.EDGE)
        self.assertIs(engine.board.edge_verdict("r1").basis, Basis.EDGE)


class TermPinningTest(EngineBuildMixin, unittest.TestCase):
    def test_revised_terms_do_not_apply_retroactively(self):
        # 旧账期允许降级，新账期禁止；请求按初次进入时间绑定旧条款。
        objs = [
            objective("edge"),
            Objective("svc", 0, 100, 5, fallback_ok=True, latency_ms=200,
                      holds_until_min=30),
            Objective("svc", 100, 200, 5, fallback_ok=False, latency_ms=150,
                      holds_until_min=10),
        ]
        intakes = [Intake(90, "r1", ("svc",), True)]  # 落在旧账期
        facts = [Fact(95, "r1", "svc", "client", "c1", 1, "received", 180, True)]
        engine = self.build(objs, intakes, facts)
        # 旧条款允许降级且 180<=200，达标；新条款不会回头套用。
        self.assertIs(
            engine.board.service_verdict("r1", "svc").result,
            UserResult.SATISFIED)


class EdgeDedupTest(EngineBuildMixin, unittest.TestCase):
    def test_edge_counts_one_failure_even_if_multiple_services_fail(self):
        objs = [
            objective("edge"),
            objective("a", fallback_ok=True, latency=200),
            objective("b", fallback_ok=True, latency=200),
        ]
        intakes = [Intake(10, "r1", ("a", "b"), True)]
        facts = [
            Fact(11, "r1", "a", "client", "c1", 1, "failed", None, False),
            Fact(11, "r1", "b", "client", "c2", 1, "failed", None, False),
        ]
        engine = self.build(objs, intakes, facts)
        self.assertEqual(engine.board.current_units(0, "a"), (1, 0))
        self.assertEqual(engine.board.current_units(0, "b"), (1, 0))
        # 共享 edge 对同一逻辑请求只计一次失败。
        self.assertEqual(engine.board.current_units(0, "edge"), (1, 0))


if __name__ == "__main__":
    unittest.main()
