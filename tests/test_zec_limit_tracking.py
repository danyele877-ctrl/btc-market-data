import unittest

from zec_limit_tracking import simulate


class LimitTrackingTests(unittest.TestCase):
    def test_conservative_fill_and_exit(self):
        plan = {"signal_at": 1, "fill_by": 240, "outcome_by": 300,
                "side": "LONG", "entry": 100, "stop": 95, "tp1": 105}
        bars = {60: (90, 110, 100), 120: (99, 106, 103), 180: (101, 106, 104)}
        self.assertEqual(simulate(plan, bars), {"status": "tp1", "filled": True, "gross_r": 1})
        bars[180] = (94, 107, 100)
        self.assertEqual(simulate(plan, bars)["status"], "stop")
        bars.pop(180)
        self.assertEqual(simulate(plan, bars)["status"], "missing_data")
        bars = {time: (101, 104, 103) for time in (120, 180, 240)}
        self.assertEqual(simulate(plan, bars)["status"], "unfilled")

    def test_short(self):
        plan = {"signal_at": 0, "fill_by": 120, "outcome_by": 180,
                "side": "SHORT", "entry": 100, "stop": 105, "tp1": 95}
        self.assertEqual(simulate(plan, {60: (94, 102, 98), 120: (94, 99, 97)})["status"], "tp1")
