import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from zec_limit_tracking import LimitTracker, simulate


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

    def test_directional_outcomes_persist_without_rewriting_existing_results(self):
        with sqlite3.connect(":memory:") as db:
            @contextmanager
            def connect():
                yield db, "?"

            tracker = LimitTracker(connect)
            now = datetime.fromtimestamp(6000, timezone.utc)
            plan = {"side": "SHORT", "quote": 100, "signal_at": 1201, "variant": "BASELINE"}
            tracker._update({}, None, [], [], now)
            before = {"status": "blocked", "directional_move_pct": {"after_5m": 9}}
            db.execute("INSERT INTO zec_limit_trials VALUES (1, 3960, ?, ?)",
                       (json.dumps([plan]), json.dumps([before])))
            db.execute("INSERT INTO zec_shadow_live_trials VALUES (1, 3901, ?, NULL, NULL)",
                       (json.dumps({**plan, "entry": 105, "stop": 110, "tp1": 95,
                                    "fill_by": 1501, "outcome_by": 3901}),))

            def candles(skip):
                return [{"completed_at": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                         "low": 90, "high": 102, "close": 95}
                        for t in range(1200, 3961, 60) if t != skip]

            tracker._update({}, None, [], candles(3600), now)
            baseline = tracker.state["records"][0]["outcomes"][0]
            self.assertEqual(baseline["status"], "blocked")
            self.assertEqual(baseline["directional_move_pct"]["after_5m"], 9)
            self.assertEqual(baseline["directional_move_pct"]["after_30m"], 5)
            self.assertNotIn("after_45m", baseline["directional_move_pct"])
            tracker._update({}, None, [], candles(None), now)
            tracker._update({}, None, [], [], now)
            for outcomes in (tracker.state["records"][0]["outcomes"][0],
                             tracker.state["shadow_records"][0]["outcome"]):
                self.assertEqual(outcomes["directional_move_pct"]["after_30m"], 5)
                self.assertEqual(outcomes["directional_move_pct"]["after_45m"], 5)
