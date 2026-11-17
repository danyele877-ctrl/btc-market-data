import unittest
from datetime import datetime, timezone

from test_zec_early_signal import INSTRUMENT_ID, NOW, valid_snapshot
from zec_early_signal import evaluate_signal
from zec_shadow_signal import evaluate_shadow_signal


def higher_timeframe_rows(seconds, count=200, rising=True):
    captured_ms = int(NOW.timestamp() * 1000)
    interval_ms = seconds * 1000
    latest_start = (captured_ms // interval_ms) * interval_ms - interval_ms
    rows = []
    for index in range(count):
        ts = latest_start - (count - index - 1) * interval_ms
        close = 100.0 + index * (0.5 if rising else -0.5)
        rows.append({
            "ts_ms": ts,
            "completed_at": datetime.fromtimestamp((ts + interval_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "open": close - 0.1, "high": close + 0.3,
            "low": close - 0.3, "close": close, "volume": 100,
        })
    return rows


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

    def test_shadow_nowcast_rejects_candles_that_age_after_snapshot_capture(self):
        snapshot = directional_snapshot("SHORT")
        baseline = evaluate_signal(snapshot, now=NOW)
        later = NOW.replace() + __import__("datetime").timedelta(minutes=4)
        shadow = evaluate_shadow_signal(snapshot, baseline=baseline, now=later)
        self.assertEqual(shadow["decision"], "ATTENDERE")
        self.assertFalse(shadow["data_quality"]["ready"])
        self.assertTrue(any("SHADOW stantia" in item for item in shadow["data_quality"]["blockers"]))

    def test_higher_timeframe_regime_requires_200_closed_contiguous_bars(self):
        snapshot = valid_snapshot()
        snapshot["predictive_history"] = {
            "xperp_1H": higher_timeframe_rows(3600),
            "xperp_4H": higher_timeframe_rows(14400),
        }
        shadow = evaluate_shadow_signal(snapshot, now=NOW)
        regime = shadow["higher_timeframe_regime"]
        self.assertEqual(regime["1H"]["status"], "OK")
        self.assertEqual(regime["4H"]["status"], "OK")
        self.assertEqual(regime["1H"]["bars"], 200)
        self.assertEqual(regime["4H"]["bars"], 200)
        self.assertEqual(regime["combined"]["regime"], "TREND RIALZISTA")

    def test_higher_timeframe_regime_is_nd_when_history_is_missing_or_short(self):
        snapshot = valid_snapshot()
        snapshot["predictive_history"] = {
            "xperp_1H": higher_timeframe_rows(3600, count=199),
            "xperp_4H": [],
        }
        shadow = evaluate_shadow_signal(snapshot, now=NOW)
        regime = shadow["higher_timeframe_regime"]
        self.assertEqual(regime["1H"]["status"], "N/D")
        self.assertEqual(regime["4H"]["status"], "N/D")
        self.assertEqual(regime["combined"]["status"], "N/D")


if __name__ == "__main__":
    unittest.main()
