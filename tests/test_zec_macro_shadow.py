import json
import sqlite3
import subprocess
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from replay_zec_macro import history

from zec_limit_tracking import LimitTracker, simulate
from zec_macro_shadow import EVENTS, event_context


EVENT = EVENTS[0]


def at(minute):
    return datetime.fromtimestamp(EVENT.at + minute * 60, timezone.utc)


def bar(minute, low, high, close):
    return {"completed_at": at(minute).isoformat(), "low": low, "high": high,
            "open": close, "close": close}


class MacroShadowTests(unittest.TestCase):
    def setUp(self):
        self.btc = [bar(i, 99, 101, 100) for i in range(-9, 2)]
        self.zec = [bar(i, 49, 51, 50) for i in range(-9, 2)]
        self.btc.extend([bar(2, 97, 100, 98), bar(3, 97, 99, 98)])
        self.zec.extend([bar(2, 46, 50, 47), bar(3, 46, 49, 47)])

    def test_rest_history_keeps_only_completed_minutes(self):
        minute = [str(EVENT.at * 1000), "10", "11", "9", "10", "1", "1", "1", "1"]
        payload = {"code": "0", "data": [minute, [*minute[:-1], "0"]]}
        with patch("replay_zec_macro.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))):
            rows = history("BTC-USDC", EVENT.at + 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts_ms"], EVENT.at * 1000)

    def test_candles_cannot_advance_the_diagnostic_before_their_close(self):
        self.assertEqual(event_context(EVENT, self.btc, self.zec, at(1))["status"], "macro_pause")
        earlier = event_context(EVENT, self.btc, self.zec, at(2))
        self.assertEqual((earlier["status"], earlier["side"]), ("watching", "SHORT"))
        self.assertNotIn("reference_limit", earlier)
        after = event_context(EVENT, self.btc, self.zec, at(3))
        self.assertEqual((after["status"], after["reference_limit"], after["reference_stop"]),
                         ("retest_observed", 49, 51))

    def test_gaps_and_reabsorption_block_the_retest(self):
        self.assertEqual(event_context(EVENT, self.btc, self.zec[:-1], at(3))["status"], "missing_candles")
        self.zec[-1] = bar(3, 47, 51, 50)
        self.assertEqual(event_context(EVENT, self.btc, self.zec, at(3))["status"], "breakout_reabsorbed")

    def test_prospective_record_preserves_first_observation(self):
        with sqlite3.connect(":memory:") as db:
            @contextmanager
            def connect():
                yield db, "?"

            tracker = LimitTracker(connect)
            first = event_context(EVENT, self.btc, self.zec, at(2))
            tracker._update({}, None, [], [], at(2), macro=first)
            later = {**first, "observed_at": at(3).timestamp()}
            tracker._update({}, None, [], [], at(3), macro=later)
            self.assertEqual(len(tracker.state["macro_records"]), 1)
            self.assertEqual(tracker.state["macro_records"][0]["observation"], first)

    def test_wider_stops_compare_at_equal_monetary_risk(self):
        plan = {"signal_at": 0, "fill_by": 120, "outcome_by": 180, "side": "LONG",
                "entry": 100, "stop": 90, "tp1": 105, "invalidation": 92, "reference_risk": 5}
        result = simulate(plan, {60: (98, 102, 99, 100), 120: (89, 101, 91, 98)})
        self.assertEqual(result["status"], "stop")
        self.assertAlmostEqual(result["net_equal_risk_r"], -1.0095)
        self.assertAlmostEqual(result["net_reference_r"], -2.019)
