import unittest

from walk_forward_zec_validation import analyze


def event(timestamp, side, outcome, btc=0.01, trend=0.02):
    return {
        "id": f"{timestamp}:{side}",
        "family": "scalp_5m",
        "timestamp_ms": timestamp,
        "side": side,
        "horizon_min": 5,
        "features": {
            "ret1": 0.001, "ret3": 0.002, "ret6": 0.003,
            "atr_pct": 0.01, "range_pct": 0.02, "volume_rel": 1.0,
            "rsi14": 0.6, "trend": trend, "rel_strength": 0.0,
            "btc_trend_20": btc,
        },
        "label": {"filled_proxy": True, "tp_before_sl": outcome},
    }


class WalkForwardValidationTests(unittest.TestCase):
    def test_requires_chronological_test_window(self):
        archive = {
            "instrument": "ZEC-USD_UM_XPERP-310530",
            "events": [
                event(i * 10 * 60_000, side, i % 2 == 0)
                for i in range(100)
                for side in ("LONG", "SHORT")
            ],
        }
        report = analyze(archive, folds=2, warmup_fraction=0.5)
        self.assertEqual(len(report["folds"]), 2)
        self.assertIn("scalp_5m|LONG|5m", report["results"])
        self.assertIn("scalp_5m|SHORT|5m", report["results"])
        self.assertIn("by_btc_regime", report["results"]["scalp_5m|LONG|5m"])
        self.assertTrue(any("Brier scores" in item for item in report["limitations"]))


    def test_legacy_archive_without_btc_feature_is_still_scored(self):
        rows = []
        for i in range(160):
            for side in ("LONG", "SHORT"):
                item = event(i * 10 * 60_000, side, i % 3 == 0)
                item["features"].pop("btc_trend_20")
                rows.append(item)
        report = analyze({"events": rows}, folds=2, warmup_fraction=0.5)
        self.assertGreater(report["scored_total"], 0)
        group = report["results"]["scalp_5m|LONG|5m"]
        self.assertGreater(group["feature_modes"]["LEGACY_NO_BTC_CONTEXT"], 0)

    def test_rejects_insufficient_history(self):
        archive = {"events": [event(i * 10 * 60_000, "LONG", True) for i in range(2)]}
        with self.assertRaises(ValueError):
            analyze(archive, folds=2)


if __name__ == "__main__":
    unittest.main()
