import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from zec_predictive_engine import (
    INSTRUMENT_ID, MIN_ANALOGS, _analogs, _clean, _features, _label,
    evaluate_predictive_engine, _write_json_atomically,
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

    def test_clean_rejects_malformed_or_misaligned_candles(self):
        start = int(datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
        valid = {"ts_ms": start, "completed_at": "2026-01-01T11:01:00Z",
                 "open": "100", "high": "101", "low": "99", "close": "100"}
        bad_range = dict(valid, high="98")
        misaligned = dict(valid, ts_ms=start + 1)
        self.assertEqual(_clean([bad_range], 60), [])
        self.assertEqual(_clean([misaligned], 60), [])

    def test_analog_model_withholds_probabilities_below_minimum(self):
        result = _analogs({"events": []}, "scalp_5m", "LONG", 5,
                          {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                           "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5, "trend": 0.0, "rel_strength": 0.0, "btc_trend_20": 0.0})
        self.assertEqual(result["status"], "IN APPRENDIMENTO")
        self.assertIsNone(result["p_limit_touch_proxy"])
        self.assertIsNone(result["p_tp_before_sl_given_touch"])

    def test_analog_distance_skips_legacy_events_with_missing_features(self):
        features = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                    "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                    "trend": 0.0, "rel_strength": 0.0, "btc_trend_20": 0.0}
        events = []
        for i in range(30):
            stored = dict(features)
            if i == 0:
                stored["rel_strength"] = None
            events.append({
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": stored,
                "label": {"filled_proxy": i < 5, "tp_before_sl": None},
            })
        result = _analogs({"events": events}, "scalp_5m", "LONG", 5, features)
        self.assertEqual(result["status"], "IN APPRENDIMENTO")
        self.assertEqual(result["archive_samples"], 30)
        self.assertEqual(result["samples"], 29)
        self.assertEqual(result["incomplete_feature_samples"], 1)
        self.assertIsNone(result["p_limit_touch_proxy"])

    def test_touch_frequency_can_be_shown_before_tp_sl_sample_threshold(self):
        features = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                    "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                    "trend": 0.0, "rel_strength": 0.0, "btc_trend_20": 0.0}
        events = []
        for i in range(30):
            events.append({
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": features,
                "label": {"filled_proxy": i < 5, "tp_before_sl": None},
            })
        result = _analogs({"events": events}, "scalp_5m", "LONG", 5, features)
        self.assertEqual(result["status"], "STIMA SPERIMENTALE")
        self.assertAlmostEqual(result["p_limit_touch_proxy"], 5/30, places=4)
        self.assertIsNone(result["p_tp_before_sl_given_touch"])
        self.assertEqual(result["tp_status"], "IN APPRENDIMENTO")

    def test_temporal_analogs_exclude_overlapping_and_unmatured_windows(self):
        features = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                    "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                    "trend": 0.0, "rel_strength": 0.0, "btc_trend_20": 0.0}
        events = []
        # Thirty non-overlapping, fully matured 5-minute windows.
        for i in range(30):
            stamp = i * 5 * 60_000
            events.append({
                "id": str(i), "timestamp_ms": stamp,
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": features,
                "label": {"filled_proxy": True, "tp_before_sl": i % 2 == 0},
            })
        # A recent immature observation and a neighbouring overlapping one.
        events.append({
            "id": "immature", "timestamp_ms": 35 * 5 * 60_000,
            "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
            "features": features,
            "label": {"filled_proxy": True, "tp_before_sl": None},
        })
        events.append({
            "id": "overlap", "timestamp_ms": 29 * 5 * 60_000 + 60_000,
            "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
            "features": features,
            "label": {"filled_proxy": True, "tp_before_sl": False},
        })
        result = _analogs(
            {"events": events}, "scalp_5m", "LONG", 5, features,
            as_of_ms=36 * 5 * 60_000,
        )
        self.assertEqual(result["archive_samples"], 32)
        self.assertEqual(result["immature_excluded_samples"], 1)
        self.assertEqual(result["overlap_excluded_samples"], 1)
        self.assertEqual(result["temporal_samples"], 30)
        self.assertEqual(result["samples"], 30)
        self.assertAlmostEqual(result["p_tp_before_sl_given_touch"], 0.5, places=4)

    def test_regime_aware_analogs_isolate_current_trend_regime(self):
        base = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                "trend": 0.0, "rel_strength": 0.0}
        events = []
        for i in range(60):
            features = dict(base)
            features["trend"] = 0.04 if i < 30 else -0.04
            events.append({
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": features,
                "label": {"filled_proxy": True, "tp_before_sl": i < 30},
            })
        current = dict(base, trend=0.04)
        result = _analogs({"events": events}, "scalp_5m", "LONG", 5, current)
        self.assertEqual(result["regime"], "UP/HIGH_VOL")
        self.assertEqual(result["regime_samples"], 30)
        self.assertFalse(result["regime_fallback_to_broad_pool"])
        self.assertEqual(result["samples"], 30)
        self.assertEqual(result["p_tp_before_sl_given_touch"], 1.0)

    def test_regime_model_falls_back_when_regime_sample_is_too_small(self):
        base = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                "trend": 0.0, "rel_strength": 0.0}
        events = []
        for i in range(40):
            features = dict(base)
            features["trend"] = 0.04 if i < 12 else -0.04
            events.append({
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": features,
                "label": {"filled_proxy": True, "tp_before_sl": i < 12},
            })
        result = _analogs({"events": events}, "scalp_5m", "LONG", 5, dict(base, trend=0.04))
        self.assertEqual(result["regime_samples"], 12)
        self.assertTrue(result["regime_fallback_to_broad_pool"])
        self.assertEqual(result["samples"], 40)

    def test_temporal_analogs_require_completed_no_touch_window(self):
        features = {"ret1": 0.0, "ret3": 0.0, "ret6": 0.0, "atr_pct": 0.01,
                    "range_pct": 0.02, "volume_rel": 1.0, "rsi14": 0.5,
                    "trend": 0.0, "rel_strength": 0.0, "btc_trend_20": 0.0}
        events = []
        for i in range(30):
            events.append({
                "id": str(i), "timestamp_ms": i * 5 * 60_000,
                "family": "scalp_5m", "side": "LONG", "horizon_min": 5,
                "features": features,
                "label": {"filled_proxy": False,
                          "fill_window_completed_at_ms": (i + 1) * 5 * 60_000},
            })
        result = _analogs(
            {"events": events}, "scalp_5m", "LONG", 5, features,
            as_of_ms=36 * 5 * 60_000,
        )
        self.assertEqual(result["temporal_samples"], 30)
        self.assertEqual(result["filled_samples"], 0)
        self.assertEqual(result["p_limit_touch_proxy"], 0.0)

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
        self.assertEqual(result["safety"]["ctVal_status"], "METADATI_API_NON_VERIFICATI")

    def test_features_include_horizon_matched_btc_context_without_imputation(self):
        rows = []
        benchmark = {}
        for i in range(21):
            ts = i * 300000
            rows.append({
                "ts_ms": ts, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.0, "volume": 10.0,
            })
            benchmark[ts] = 50000.0 * (1.001 ** i)
        features = _features(rows, 20, benchmark)
        self.assertAlmostEqual(features["btc_trend_20"], 1.001 ** 20 - 1.0, places=8)
        self.assertAlmostEqual(features["rel_strength"], -(1.001 ** 3 - 1.0), places=8)
        without_benchmark = _features(rows, 20)
        self.assertIsNone(without_benchmark["btc_trend_20"])

    def test_volume_relative_uses_actual_nineteen_bar_baseline(self):
        rows = []
        for i in range(21):
            rows.append({
                "ts_ms": i * 300000,
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.0, "volume": 20.0 if i == 20 else 10.0,
            })
        features = _features(rows, 20)
        self.assertAlmostEqual(features["volume_rel"], 2.0, places=8)

    def test_predictive_archive_atomic_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.json"
            path.write_text('{"previous":true}', encoding="utf-8")
            with patch("zec_predictive_engine.os.replace", side_effect=OSError("simulated")):
                with self.assertRaises(OSError):
                    _write_json_atomically(path, {"new": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"previous": True})
            self.assertEqual(list(Path(tmp).glob(".archive.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
