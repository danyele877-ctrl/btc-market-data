import unittest

from analyze_zec_prediction_tracking import analyze_archive


def prediction(ts, baseline_side, baseline_decision, shadow_side, shadow_decision, regime="TREND"):
    outcomes = {}
    for engine in ("baseline", "shadow"):
        outcomes[engine] = {
            "5": {
                "status": "MATURO",
                "barrier_outcome": "TP1" if ts == 0 else "SL",
                "directional_return_pct": 0.2 if ts == 0 else -0.1,
                "mfe_atr": 1.7 if ts == 0 else 0.4,
                "mae_atr": 0.2 if ts == 0 else 1.2,
            }
        }
    return {
        "entry_close_ms": ts,
        "baseline": {"direction": baseline_side, "decision": baseline_decision},
        "shadow": {
            "direction": shadow_side, "decision": shadow_decision,
            "regime": {"status": regime},
        },
        "outcomes": outcomes,
    }


class PredictionTrackingAnalysisTests(unittest.TestCase):
    def test_reports_raw_and_non_overlapping_outcomes_separately(self):
        archive = {
            "schema_version": 1,
            "instrument": "ZEC-USD_UM_XPERP-310530",
            "updated_at": "2026-11-17T12:00:00Z",
            "predictions": [
                prediction(0, "LONG", "ATTENDERE", "LONG", "LONG"),
                prediction(60_000, "SHORT", "SHORT", "SHORT", "ATTENDERE"),
            ],
        }
        report = analyze_archive(archive)
        baseline = report["engines"]["baseline"]
        self.assertEqual(baseline["predictions_with_direction"], 2)
        self.assertEqual(baseline["confirmed_directional_triggers"], 1)
        self.assertEqual(baseline["by_direction"]["LONG"]["5"]["all_observations"]["matured"], 1)
        self.assertEqual(baseline["by_direction"]["LONG"]["5"]["non_overlapping"]["matured"], 1)
        self.assertEqual(baseline["by_direction"]["SHORT"]["5"]["all_observations"]["matured"], 1)
        self.assertEqual(baseline["by_direction"]["SHORT"]["5"]["non_overlapping"]["matured"], 0)
        self.assertEqual(baseline["confirmed_triggers_by_direction"]["SHORT"]["5"]["all_observations"]["matured"], 1)

    def test_rejects_wrong_archive_instrument(self):
        with self.assertRaises(ValueError):
            analyze_archive({"schema_version": 1, "instrument": "ZEC-USDT-SWAP", "predictions": []})


if __name__ == "__main__":
    unittest.main()
