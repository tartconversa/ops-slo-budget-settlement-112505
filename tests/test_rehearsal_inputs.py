import unittest

from materials import read_feed


class InputsTest(unittest.TestCase):
    def test_evidence_and_authorizations_cross_periods(self):
        intake = {r["request"]: r for r in read_feed("intake")}
        objectives = list(read_feed("objectives"))
        facts = list(read_feed("facts"))
        actions = list(read_feed("changes"))
        for row in facts:
            request = intake[row["request"]]
            self.assertIn(row["service"], request["services"])
            self.assertGreaterEqual(row["at"], request["minute"])
        before, after = [r for r in objectives if r["scope"] == "summary"]
        self.assertNotEqual(before["fallback_ok"], after["fallback_ok"])
        correction = [r for r in facts if r["source"] == "client" and r["ref"] == "a5"]
        self.assertEqual([r["revision"] for r in correction], [1, 2])
        self.assertLess(intake["u01"]["minute"], before["month_end"])
        self.assertGreater(correction[-1]["at"], before["month_end"])
        self.assertTrue(any(r["kind"] == "begin" and r["at"] < correction[-1]["at"] for r in actions))

