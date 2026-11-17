import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zec_prediction_tracking import INSTRUMENT_ID, update_prediction_tracking


T0 = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


def make_snapshot(captured, count):
    end_ms = int(captured.timestamp() * 1000)
    latest_start = end_ms - 60_000
    start = latest_start - (count - 1) * 60_000
    rows = []
    for index in range(count):
        ts = start + index * 60_000
        close = 100.0 + index * 0.01
        rows.append({
            "ts_ms": ts,
            "completed_at": datetime.fromtimestamp((ts + 60_000) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "open": close - 0.01, "high": close + 0.30,
            "low": close - 0.30, "close": close, "volume": 100,
        })
    return {
        "schema_version": 3,
        "source_status": "ok",
        "source_captured_at": captured.isoformat().replace("+00:00", "Z"),
        "market": {"zec_xperp_ticker": {"first": {"instId": INSTRUMENT_ID, "last": "100"}}},
        "predictive_history": {"xperp_1m": rows},
        "zec_early_signal": {"candidate_direction": "SHORT", "decision": "ATTENDERE", "status": "ATTENDERE",
                             "data_quality": {"blockers": ["book non disponibile"]}},
        "zec_shadow_signal": {"candidate_direction": "LONG", "decision": "ATTENDERE", "status": "ATTENDERE",
                              "score": 12.5, "components": {"price": {}, "relative_strength": {}},
                              "higher_timeframe_regime": {"combined": {"status": "N/D"}},
                              "data_quality": {"blockers": ["book non disponibile"]}},
    }


class PredictionTrackingTests(unittest.TestCase):
    def test_records_both_engines_without_orders_or_sizing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tracking.json"
            result = update_prediction_tracking(make_snapshot(T0, 60), path, now=T0)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["predictions_added"], 1)
            self.assertFalse(result["orders_enabled"])
            self.assertFalse(result["position_sizing_enabled"])
            import json
            archive = json.loads(path.read_text())
            row = archive["predictions"][0]
            self.assertEqual(row["instrument"], INSTRUMENT_ID)
            self.assertEqual(row["baseline"]["direction"], "SHORT")
            self.assertEqual(row["shadow"]["direction"], "LONG")
            self.assertEqual(len(row["snapshot_sha256"]), 64)

    def test_matures_forward_outcomes_only_when_full_path_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tracking.json"
            first = make_snapshot(T0, 60)
            update_prediction_tracking(first, path, now=T0)
            later = T0 + timedelta(minutes=40)
            second = make_snapshot(later, 100)
            result = update_prediction_tracking(second, path, now=later)
            import json
            archive = json.loads(path.read_text())
            original = next(row for row in archive["predictions"] if row["captured_at"] == T0.isoformat().replace("+00:00", "Z"))
            for horizon in (5, 15, 30, 40):
                self.assertEqual(original["outcomes"]["baseline"][str(horizon)]["status"], "MATURO")
                self.assertEqual(original["outcomes"]["shadow"][str(horizon)]["status"], "MATURO")
            self.assertGreaterEqual(result["mature_outcomes_total"], 8)

    def test_rejects_wrong_instrument_and_stale_snapshot(self):
        snapshot = make_snapshot(T0, 60)
        snapshot["market"]["zec_xperp_ticker"]["first"]["instId"] = "ZEC-USDT-SWAP"
        self.assertEqual(update_prediction_tracking(snapshot, now=T0)["status"], "BLOCCATO")
        stale = make_snapshot(T0, 60)
        self.assertEqual(update_prediction_tracking(stale, now=T0 + timedelta(minutes=4))["status"], "BLOCCATO")


if __name__ == "__main__":
    unittest.main()
