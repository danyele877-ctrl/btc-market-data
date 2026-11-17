import unittest

from test_zec_early_signal import INSTRUMENT_ID, NOW, valid_snapshot
from zec_early_signal import evaluate_signal
from zec_shadow_signal import evaluate_shadow_signal


def directional_snapshot(direction, breakout_volume=500):
    snapshot = valid_snapshot()
    sign = 1 if direction == "LONG" else -1
    for instrument, base, step_pct in (
        ("ZEC-USD_UM_XPERP", 100.0, 0.0015),
        ("ZEC-USDC", 100.0, 0.0015),
        ("BTC-USDC", 1000.0, 0.0010),
    ):
        for timeframe in ("1m", "5m"):
            rows = snapshot["candles_1m_5m"][instrument][timeframe]["tail"]
            step = base * step_pct
            for index, row in enumerate(rows):
                close = base + sign * step * index
                row.update({
                    "open": close - sign * step * 0.2,
                    "high": close + step * 0.6,
                    "low": close - step * 0.6,
                    "close": close,
                    "volume": 100,
                })
            if instrument == "ZEC-USD_UM_XPERP" and timeframe == "5m":
                rows[-1]["volume"] = breakout_volume
    micro = snapshot["microstructure"]
    micro["cvd"].update({"1m": sign * 10, "5m": sign * 20})
    micro["book_imbalance"]["imbalance_top10"] = sign * 0.8
    micro["websocket"]["book_history"] = [
        {"ts_ms": int(NOW.timestamp() * 1000) - 5_000, "imbalance_top10": sign * 0.2},
        {"ts_ms": int(NOW.timestamp() * 1000), "imbalance_top10": sign * 0.8},
    ]
    micro["open_interest_history"] = [
        {"ts_ms": int(NOW.timestamp() * 1000) - 120_000, "oi_contracts": "100", "price_usd": "100", "inst_id": INSTRUMENT_ID},
        {"ts_ms": int(NOW.timestamp() * 1000), "oi_contracts": "101", "price_usd": str(100 + sign), "inst_id": INSTRUMENT_ID},
    ]
    return snapshot


class ZecShadowSignalTests(unittest.TestCase):
    def test_shadow_mirrors_long_and_short_without_changing_baseline(self):
        for side in ("LONG", "SHORT"):
            snapshot = directional_snapshot(side)
            baseline = evaluate_signal(snapshot, now=NOW)
            baseline_before = dict(baseline)
            shadow = evaluate_shadow_signal(snapshot, baseline=baseline, now=NOW)
            self.assertEqual(shadow["status"], "TRIGGER CONFERMATO")
            self.assertEqual(shadow["decision"], side)
            self.assertEqual(shadow["candidate_direction"], side)
            self.assertEqual(shadow["baseline_comparison"]["status"], baseline_before["status"])
            self.assertTrue(shadow["baseline_unchanged"])
            self.assertEqual(shadow["validation_status"], "IN RACCOLTA — NESSUNA VALIDAZIONE")
            self.assertFalse(shadow["orders_enabled"])
            self.assertFalse(shadow["position_sizing_enabled"])
            self.assertEqual(baseline, baseline_before)

    def test_shadow_vetoes_short_near_support_without_breakdown(self):
        snapshot = directional_snapshot("SHORT", breakout_volume=100)
        baseline = evaluate_signal(snapshot, now=NOW)
        shadow = evaluate_shadow_signal(snapshot, baseline=baseline, now=NOW)
        self.assertEqual(shadow["candidate_direction"], "SHORT")
        self.assertEqual(shadow["decision"], "ATTENDERE")
        self.assertIn("VETO SHORT", shadow["structure"]["veto"])

    def test_shadow_vetoes_long_near_resistance_without_breakout(self):
        snapshot = directional_snapshot("LONG", breakout_volume=100)
        baseline = evaluate_signal(snapshot, now=NOW)
        shadow = evaluate_shadow_signal(snapshot, baseline=baseline, now=NOW)
        self.assertEqual(shadow["candidate_direction"], "LONG")
        self.assertEqual(shadow["decision"], "ATTENDERE")
        self.assertIn("VETO LONG", shadow["structure"]["veto"])

    def test_shadow_always_returns_long_or_short_candidate(self):
        baseline = evaluate_signal({}, now=NOW)
        shadow = evaluate_shadow_signal({}, baseline=baseline, now=NOW)
        self.assertIn(shadow["candidate_direction"], ("LONG", "SHORT"))
        self.assertEqual(shadow["decision"], "ATTENDERE")
        self.assertFalse(shadow["data_quality"]["ready"])


if __name__ == "__main__":
    unittest.main()
