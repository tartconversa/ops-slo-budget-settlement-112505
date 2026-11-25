"""用演练资料做端到端断言，锁定手工核算的关键结果与跨月口径。"""
import unittest
from pathlib import Path

from slo_budget.cli import build_engine
from slo_budget.model import ChangeState, UserResult

MATERIALS = Path(__file__).resolve().parent.parent / "materials"


class RehearsalEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = build_engine(MATERIALS)

    def test_restated_failures_match_manual_settlement(self):
        e = self.engine
        # 账期 0
        self.assertEqual(e.board.current_units(0, "edge"), (3, 0))
        self.assertEqual(e.board.current_units(0, "summary"), (1, 0))
        self.assertEqual(e.board.current_units(0, "search"), (2, 0))
        # 账期 100：u08 仍是缺证预留，u06 一次失败
        self.assertEqual(e.board.current_units(100, "edge"), (1, 1))
        self.assertEqual(e.board.current_units(100, "summary"), (1, 1))
        self.assertEqual(e.board.current_units(100, "search"), (0, 0))

    def test_unknown_and_held_are_never_counted_as_zero_failure(self):
        e = self.engine
        # u08 有客户端渠道但只到 unknown：仍处于 held 预留。
        self.assertIs(
            e.board.service_verdict("u08", "summary").result, UserResult.HELD)

    def test_client_correction_drives_restatement(self):
        e = self.engine
        u01 = e.board.service_verdict("u01", "summary")
        self.assertIs(u01.result, UserResult.FAILED)
        self.assertTrue(u01.restated)
        # u01 search 的客户端失败被撤销后回到边缘达标口径。
        self.assertIs(
            e.board.service_verdict("u01", "search").result,
            UserResult.SATISFIED)

    def test_change_desk_final_states(self):
        tickets = self.engine.desk.tickets
        self.assertIs(tickets["chg-k"].state, ChangeState.FINISHED)
        self.assertIs(tickets["chg-n"].state, ChangeState.WITHDRAWN)
        # chg-m 经回流后因独立证据释放重新获批；chg-r/chg-s 证据亏空下保持排队。
        self.assertIs(tickets["chg-m"].state, ChangeState.APPLIED)
        self.assertIs(tickets["chg-r"].state, ChangeState.PENDING)
        self.assertIs(tickets["chg-s"].state, ChangeState.PENDING)
        self.assertEqual(self.engine.desk.pending_order(), ["chg-r", "chg-s"])

    def test_authorization_snapshots_preserve_then_basis(self):
        snap = self.engine.desk.tickets["chg-k"].snapshots[0]
        self.assertEqual(snap.at, 88)
        self.assertIn("已有证据", snap.basis)

    def test_three_views_available_in_reports(self):
        from slo_budget.reports import Reporter
        reporter = Reporter(self.engine)
        rows = { (r["period_start"], r["scope"]): r for r in reporter.budget_table()}
        edge0 = rows[(0, "edge")]
        self.assertEqual(edge0["restated_failures"], 3)
        self.assertEqual(edge0["change_committed"], 2)
        self.assertEqual(edge0["live_available"], 1)
        # 报表保留健康比例但同时给出可决策判断，而非只给一个比例。
        self.assertIn("decision", edge0)

    def test_reconciliation_explains_late_evidence(self):
        from slo_budget.reports import Reporter
        items = Reporter(self.engine).reconciliation()
        requests = {item.request for item in items}
        self.assertIn("u01", requests)
        self.assertIn("u02", requests)


if __name__ == "__main__":
    unittest.main()
