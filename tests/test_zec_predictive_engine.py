import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from zec_predictive_engine import (
    INSTRUMENT_ID, MIN_ANALOGS, _analogs, _clean, _features, _label,
    evaluate_predictive_engine,
)


class PredictiveEngineTests(unittest.TestCase):
    def test_clean_rejects_open_or_future_candles(self):
        captured = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        row = {
            "ts_ms": int(datetime(2026, 1, 1, 11, 59, tzinfo=timezone.utc).timestamp() * 1000),
            "completed_at": "2026-01-01T12:01:00Z",
            "open": "100", "high": "101", "low": "99", "close": "100",
        }
        self.assertEqual(_clean([row], 60, captured), [])

    def test_analog_model_withholds_probabilities_below_minimum(self):
        result = _analogs({"events": []}, "scalp_5m", "LONG", 5,
                          {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                           "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5, "trend": 0.0, "rel_strength": 0.0})
        self.assertEqual(result["status"], "IN APPRENDIMENTO")
        self.assertIsNone(result["p_limit_touch_proxy"])
        self.assertIsNone(result["p_tp_before_sl_given_touch"])

    def test_filled_limit_is_recorded_before_longer_tp_sl_horizon_matures(self):
        entry = {
            "ts_ms": 1000, "_close_ms": 61000, "open": 100.0,
            "high": 100.1, "low": 99.9, "close": 100.0, "_atr": 1.0,
        }
        first = {
            "ts_ms": 61000, "_close_ms": 121000, "open": 100.0,
            "high": 100.2, "low": 99.7, "close": 100.0,
        }
        partial = _label(entry, [first], "LONG", 40, 60, 5)
        self.assertTrue(partial["filled_proxy"])
        self.assertIsNone(partial["tp_before_sl"])

        future = [first]
        for minute in range(2, 41):
            high = 102.0 if minute == 30 else 100.2
            future.append({
                "ts_ms": 61000 + minute*60000 - 60000,
                "_close_ms": 61000 + minute*60000,
                "open": 100.0, "high": high, "low": 99.9, "close": 100.0,
            })
        matured = _label(entry, future, "LONG", 40, 60, 5)
        self.assertTrue(matured["filled_proxy"])
        self.assertTrue(matured["tp_before_sl"])
        self.assertIsNotNone(matured["outcome_at_ms"])

    def test_engine_pins_exact_instrument_and_never_enables_orders(self):
        snapshot = {"market": {"zec_xperp_ticker": {"first": {"instId": "ZEC-USDT-SWAP"}}}}
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate_predictive_engine(snapshot, Path(tmp) / "archive.json")
        self.assertEqual(result["status"], "BLOCCATO")
        self.assertFalse(result["orders_enabled"])

    def test_engine_refuses_wrong_contract(self):
        snapshot = {"market": {"zec_xperp_ticker": {"first": {"instId": INSTRUMENT_ID}}}}
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate_predictive_engine(snapshot, Path(tmp) / "archive.json")
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["safety"]["position_sizing_enabled"])
        self.assertEqual(result["safety"]["ctVal_status"], "NON RISOLTO")


if __name__ == "__main__":
    unittest.main()
