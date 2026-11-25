"""变更额度台与预算台账测试：占用、回流、争用顺序、亏空、授权依据。"""
import unittest

from slo_budget.model import (
    ChangeAction,
    ChangeState,
    Fact,
    Intake,
    Objective,
    UserResult,
)
from slo_budget.engine import SettlementEngine


def make_engine():
    engine = SettlementEngine()
    for obj in (
        Objective("edge", 0, 100, 6, None, None, 10),
        Objective("svc", 0, 100, 3, True, 200, 10),
        Objective("edge", 100, 200, 5, None, None, 10),
        Objective("svc", 100, 200, 4, False, 150, 10),
    ):
        engine.register_objective(obj)
    return engine


def change(at, action, kind, period, scopes, units=1):
    return ChangeAction(at, action, kind, period, tuple(scopes), units)


class QuotaDeskTest(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def test_approved_change_holds_margin_before_use(self):
        self.engine.register_change(change(5, "c1", "apply", 0, ["edge", "svc"]))
        edge = self.engine.ledger.budget_for(0, "edge")
        self.assertEqual(edge.change_committed, 1)
        self.assertEqual(edge.live_available, 5)
        self.assertEqual(self.engine.desk.tickets["c1"].state,
                         ChangeState.APPLIED)

    def test_application_blocked_when_any_scope_short(self):
        # edge 6、svc 3；申请 4 个单位，svc 不够。
        self.engine.register_change(change(5, "c1", "apply", 0, ["edge", "svc"], 4))
        ticket = self.engine.desk.tickets["c1"]
        self.assertIs(ticket.state, ChangeState.PENDING)
        self.assertIn("svc", ticket.last_decision)
        self.assertEqual(
            self.engine.ledger.budget_for(0, "svc").change_committed, 0)

    def test_started_change_is_protected_and_keeps_authorization_basis(self):
        self.engine.register_change(change(5, "c1", "apply", 0, ["edge", "svc"]))
        self.engine.register_change(change(6, "c1", "begin", 0, ["edge", "svc"]))
        ticket = self.engine.desk.tickets["c1"]
        self.assertIs(ticket.state, ChangeState.RUNNING)
        basis_before = list(ticket.snapshots)
        # 制造一次证据更正，运行中的变更不得被回流。
        self.engine.register_intake(Intake(7, "r1", ("svc",), True))
        self.engine.register_fact(
            Fact(8, "r1", "svc", "client", "x1", 1, "failed", None, False))
        self.assertIs(ticket.state, ChangeState.RUNNING)
        self.assertEqual(ticket.snapshots, basis_before)

    def test_withdraw_releases_unstarted_hold(self):
        self.engine.register_change(change(5, "c1", "apply", 0, ["edge", "svc"]))
        self.engine.register_change(change(7, "c1", "withdraw", 0, ["edge", "svc"]))
        self.assertIs(self.engine.desk.tickets["c1"].state,
                      ChangeState.WITHDRAWN)
        self.assertEqual(
            self.engine.ledger.budget_for(0, "edge").change_committed, 0)

    def test_running_change_cannot_be_withdrawn(self):
        self.engine.register_change(change(5, "c1", "apply", 0, ["edge", "svc"]))
        self.engine.register_change(change(6, "c1", "begin", 0, ["edge", "svc"]))
        with self.assertRaises(ValueError):
            self.engine.register_change(
                change(7, "c1", "withdraw", 0, ["edge", "svc"]))

    def test_pending_applications_reviewed_in_arrival_order(self):
        # 占掉 svc 全部 3 个单位。
        self.engine.register_change(change(1, "c1", "apply", 0, ["svc"], 3))
        # 后续两个申请都只能排队，顺序按到达。
        self.engine.register_change(change(2, "c2", "apply", 0, ["svc"], 1))
        self.engine.register_change(change(3, "c3", "apply", 0, ["svc"], 1))
        self.assertEqual(self.engine.desk.pending_order(), ["c2", "c3"])
        # c1 撤单释放 3 个单位，c2 先于 c3 获得授权。
        self.engine.register_change(change(4, "c1", "withdraw", 0, ["svc"], 3))
        self.assertIs(self.engine.desk.tickets["c2"].state,
                      ChangeState.APPLIED)
        self.assertIs(self.engine.desk.tickets["c3"].state,
                      ChangeState.APPLIED)


class ReflowAndDeficitTest(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def _failing_request(self, request, minute, scope="svc"):
        self.engine.register_intake(Intake(minute, request, (scope,), True))
        self.engine.register_fact(
            Fact(minute + 1, request, scope, "client", f"{request}-c", 1,
                 "failed", None, False))

    def test_evidence_correction_reflows_mutable_change_without_oscillation(self):
        # 先批准一个 svc 变更。
        self.engine.register_change(change(5, "c1", "apply", 0, ["svc"], 1))
        self.assertIs(self.engine.desk.tickets["c1"].state,
                      ChangeState.APPLIED)
        # 连续制造失败，把 svc 预算（3）打满并使 c1 失去余量。
        for i in range(3):
            self._failing_request(f"r{i}", 10 + i)
        ticket = self.engine.desk.tickets["c1"]
        self.assertIs(ticket.state, ChangeState.PENDING)
        self.assertIsNotNone(ticket.reflow_epoch)
        # 同批回流释放的额度不会把它立刻授权回来。
        self.assertIn("回流", ticket.last_decision)

    def test_deficit_scope_blocks_new_authorization(self):
        # svc 预算 3，制造 4 次失败形成亏空。
        for i in range(4):
            self._failing_request(f"d{i}", 10 + i)
        svc = self.engine.ledger.budget_for(0, "svc")
        self.assertEqual(svc.deficit, 1)
        self.engine.register_change(change(40, "c9", "apply", 0, ["svc"], 1))
        self.assertIs(self.engine.desk.tickets["c9"].state,
                      ChangeState.PENDING)
        self.assertIn("亏空", self.engine.desk.tickets["c9"].last_decision)

    def test_withdraw_does_not_grant_new_quota_to_deficit_scope(self):
        for i in range(4):
            self._failing_request(f"d{i}", 10 + i)
        # 另一个 edge 变更先占用再撤销，svc 仍亏空，svc 新申请不得借此获批。
        self.engine.register_change(change(40, "ce", "apply", 0, ["edge"], 1))
        self.engine.register_change(change(41, "ce", "withdraw", 0, ["edge"], 1))
        self.engine.register_change(change(42, "cs", "apply", 0, ["svc"], 1))
        self.assertIs(self.engine.desk.tickets["cs"].state,
                      ChangeState.PENDING)

    def test_running_change_emits_risk_when_scope_hits_deficit(self):
        self.engine.register_change(change(5, "cr", "apply", 0, ["svc"], 1))
        self.engine.register_change(change(6, "cr", "begin", 0, ["svc"], 1))
        for i in range(4):
            self._failing_request(f"d{i}", 10 + i)
        risks = [e for e in self.engine.log
                 if e.stream == "changes" and e.kind == "running-risk"]
        self.assertTrue(risks)
        self.assertIn("cr", risks[-1].ref)
        # 运行中变更依旧保持 running，授权依据不被回改。
        self.assertIs(self.engine.desk.tickets["cr"].state,
                      ChangeState.RUNNING)


class ThreeViewsTest(unittest.TestCase):
    def test_live_restated_and_authorization_are_distinct(self):
        engine = make_engine()
        engine.register_change(change(5, "c1", "apply", 0, ["svc"], 1))
        engine.register_intake(Intake(10, "r1", ("svc",), True))
        # 补证期满前是预留：实时占用含预留，重述失败仍为 0。
        svc = engine.ledger.budget_for(0, "svc")
        self.assertEqual(svc.reserved_units, 1)
        self.assertEqual(svc.failure_units, 0)
        self.assertEqual(svc.live_occupied, 2)  # 1 变更 + 1 预留
        # 授权快照独立保留当时数字。
        snap = engine.desk.tickets["c1"].snapshots[0]
        self.assertEqual(snap.occupied["svc"], 1)
        engine.advance_to(25)  # 补证期满，未知保守结算为失败
        self.assertEqual(svc.failure_units, 1)
        # 历史授权快照不被后续结算回改。
        self.assertEqual(
            engine.desk.tickets["c1"].snapshots[0].occupied["svc"], 1)


if __name__ == "__main__":
    unittest.main()
