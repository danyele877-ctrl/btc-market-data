import unittest

from analyze_zec_directional_bias import analyze, _non_overlapping, _paired_events


def event(timestamp, side, horizon=15, filled=True, win=True, family="scalp_5m"):
    return {
        "timestamp_ms": timestamp,
        "family": family,
        "side": side,
        "horizon_min": horizon,
        "label": {
            "filled_proxy": filled,
            "tp_before_sl": win if filled else None,
        },
    }


class DirectionalBiasAuditTests(unittest.TestCase):
    def test_pairs_long_and_short_at_same_timestamp(self):
        events = [
            event(0, "LONG"),
            event(0, "SHORT", win=False),
            event(300000, "LONG"),
        ]
        pairs = _paired_events(events, "scalp_5m", 15)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["timestamp_ms"], 0)

    def test_non_overlapping_sample_respects_full_horizon(self):
        pairs = [{"timestamp_ms": ts} for ts in (0, 300000, 900000, 1200000, 1800000)]
        selected = _non_overlapping(pairs, 15)
        self.assertEqual([item["timestamp_ms"] for item in selected], [0, 900000, 1800000])

    def test_report_keeps_fill_and_tp_rates_separate_by_side(self):
        events = []
        for ts in (0, 900000, 1800000):
            events.extend([
                event(ts, "LONG", filled=True, win=True),
                event(ts, "SHORT", filled=True, win=False),
            ])
        report = analyze({"instrument": "ZEC-USD_UM_XPERP-310530", "updated_at": "2026-11-17T00:00:00Z", "events": events})
        metrics = report["families"]["scalp_5m"]["15"]
        self.assertEqual(metrics["non_overlapping_paired_events"], 3)
        self.assertEqual(metrics["LONG"]["fill_rate_proxy"], 1.0)
        self.assertEqual(metrics["LONG"]["tp_before_sl_rate_conditional_on_fill"], 1.0)
        self.assertEqual(metrics["SHORT"]["tp_before_sl_rate_conditional_on_fill"], 0.0)
        self.assertTrue(report["limitations"])


if __name__ == "__main__":
    unittest.main()
