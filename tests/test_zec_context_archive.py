import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zec_context_archive import INSTRUMENT_ID, _closed_1m, _dedupe_movement_events, _mature_setups, _movement_candidates, backfill_context_archive, update_context_archive


NOW = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


def bar(start, close, high=None, low=None):
    return {
        "ts_ms": int(start.timestamp() * 1000),
        "completed_at": (start + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "open": close, "high": high if high is not None else close + 0.2,
        "low": low if low is not None else close - 0.2, "close": close,
    }


def snapshot():
    bid, ask = 99.9, 100.1
    return {
        "schema_version": 3,
        "source_status": "ok",
        "source_captured_at": NOW.isoformat().replace("+00:00", "Z"),
        "market": {
            "zec_xperp_ticker": {
                "exchange_age_seconds": 1,
                "first": {"instId": INSTRUMENT_ID, "last": "100"},
            },
            "zec_xperp_open_interest": {
                "first": {"instId": INSTRUMENT_ID, "oi": "1000", "oiUsd": "100000"},
            },
        },
        "microstructure": {
            "websocket": {
                "connected": True, "book_valid": True,
                "message_age_seconds": 1, "trade_age_seconds": 1, "book_age_seconds": 1,
                "book": {
                    "best_bid": bid, "best_ask": ask,
                    "imbalance_top5": 0.1, "imbalance_top10": 0.2, "imbalance_top20": 0.3,
                },
                "book_history": [
                    {"ts_ms": int(NOW.timestamp() * 1000)-10000, "imbalance_top10": 0.1},
                    {"ts_ms": int(NOW.timestamp() * 1000), "imbalance_top10": 0.2},
                ],
                "trade_gap_count": 0, "book_gap_count": 0,
            },
            "cvd": {
                "ready_1m": True, "ready_5m": True, "ready_15m": True,
                "1m": 12, "5m": 30, "15m": 60,
            },
        },
        "zec_early_signal": {"status": "ATTENDERE", "decision": "ATTENDERE"},
        "zec_xperp_history": {"1m": {"indicators": {"atr14": 1.0}}},
        "predictive_history": {"xperp_1m": [bar(NOW-timedelta(minutes=1), 100)]},
    }


class ZecContextArchiveTests(unittest.TestCase):
    def test_micro_observation_is_saved_and_same_minute_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            first = update_context_archive(snapshot(), path, now=NOW)
            self.assertEqual(first["observations_total"], 1)
            second = update_context_archive(snapshot(), path, now=NOW)
            self.assertEqual(second["observations_total"], 1)
            self.assertEqual(second["observations_added"], 0)

    def test_bulk_backfill_deduplicates_observations_by_minute(self):
        first = snapshot()
        second = snapshot()
        second_time = NOW + timedelta(seconds=30)
        second["source_captured_at"] = second_time.isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            result = backfill_context_archive([(NOW, first), (second_time, second)], path)
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            repeated = backfill_context_archive([(NOW, first), (second_time, second)], path)
        self.assertEqual(result["observations_total"], 1)
        self.assertEqual(len(reloaded["observations"]), 1)
        self.assertEqual(repeated["observations_total"], 1)

    def test_movement_episode_deduplication_applies_directional_cooldowns(self):
        base = int(NOW.timestamp() * 1000)
        events = [
            {"id": "a", "entry_close_ms": base, "direction": "LONG"},
            {"id": "b", "entry_close_ms": base + 60000, "direction": "LONG"},
            {"id": "c", "entry_close_ms": base + 16*60000, "direction": "LONG"},
            {"id": "d", "entry_close_ms": base + 17*60000, "direction": "SHORT"},
            {"id": "e", "entry_close_ms": base + 22*60000, "direction": "SHORT"},
        ]
        result = _dedupe_movement_events(events)
        self.assertEqual([event["id"] for event in result], ["a", "c", "e"])

    def test_large_move_records_preceding_cvd_book_and_btc_context(self):
        rows = []
        btc_rows = []
        start = NOW - timedelta(minutes=25)
        for i in range(25):
            candle_start = start + timedelta(minutes=i)
            zec_close = 102.0 if i == 24 else 100.0
            rows.append(bar(candle_start, zec_close,
                            high=zec_close + 0.1, low=zec_close - 0.1))
            btc_rows.append(bar(candle_start, 1000.0,
                                high=1000.1, low=999.9))
        captured_at = NOW - timedelta(seconds=30)
        observation = {
            "minute_ms": int(captured_at.timestamp() // 60 * 60000),
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "cvd_1m_contracts": 10, "cvd_5m_contracts": 40, "cvd_15m_contracts": 70,
            "imbalance_top10": 0.4, "book_delta_top10": 0.2,
            "spread_bps": 1.5, "oi_contracts": 1000, "oi_valid": True,
            "funding_rate": -0.0001,
        }
        value = snapshot()
        value["predictive_history"] = {"xperp_1m": rows, "btc_1m": btc_rows}
        events = _movement_candidates(value, [observation], NOW)
        self.assertTrue(events)
        self.assertEqual(events[-1]["direction"], "LONG")
        self.assertEqual(events[-1]["features"]["cvd_alignment"], "concorde")
        self.assertEqual(events[-1]["features"]["book_alignment"], "concorde")
        self.assertEqual(events[-1]["features"]["btc_return_5m_pct"], 0.0)
        self.assertEqual(events[-1]["event_candle_completed_at"], NOW.isoformat().replace("+00:00", "Z"))
        self.assertEqual(events[-1]["context_observation_at"], captured_at.isoformat().replace("+00:00", "Z"))
        self.assertIn("non dimostrano causalità", events[-1]["causality_note"])

    def test_truncated_forward_path_is_not_matured(self):
        entry_time = int((NOW - timedelta(minutes=10)).timestamp() * 1000)
        setup = {
            "entry_close_ms": entry_time, "entry_reference": 100,
            "atr_reference": 1, "direction": "LONG", "outcomes": {},
        }
        # The visible tail starts several minutes after entry; it cannot prove the full path.
        future = [bar(NOW - timedelta(minutes=7-i), 100) for i in range(5)]
        matured = _mature_setups([setup], _closed_1m(future, NOW))
        self.assertEqual(matured, 0)
        self.assertEqual(setup["outcomes"], {})

    def test_stale_or_wrong_contract_snapshot_is_blocked(self):
        value = snapshot()
        value["market"]["zec_xperp_ticker"]["first"]["instId"] = "ZEC-USDT-SWAP"
        with tempfile.TemporaryDirectory() as tmp:
            result = update_context_archive(value, Path(tmp) / "context.json", now=NOW)
        self.assertEqual(result["status"], "BLOCCATO")
        self.assertEqual(result["observations_total"] if "observations_total" in result else 0, 0)

    def test_confirmed_trigger_is_recorded_once_per_episode(self):
        value = snapshot()
        value["zec_early_signal"] = {
            "status": "TRIGGER CONFERMATO", "decision": "LONG",
            "scores": {"net": 70, "groups": {"order_flow": {"score": 0.8}}},
            "confidence": {"score": 70}, "invalidation": {"price": 98},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            first = update_context_archive(value, path, now=NOW)
            second = update_context_archive(value, path, now=NOW)
        self.assertEqual(first["setups_added"], 1)
        self.assertEqual(second["setups_total"], 1)

    def test_setup_outcome_matures_with_conservative_same_bar_rule(self):
        entry_time = int(NOW.timestamp() * 1000)
        setup = {
            "entry_close_ms": entry_time, "entry_reference": 100,
            "atr_reference": 1, "direction": "LONG", "outcomes": {},
        }
        future = []
        for i in range(5):
            start = NOW + timedelta(minutes=i)
            # On the fifth completed minute, both TP1=101.5 and SL=99 are touched.
            future.append(bar(start, 100, high=102 if i == 4 else 100.5,
                              low=98 if i == 4 else 99.5))
        matured = _mature_setups([setup], _closed_1m(future, NOW + timedelta(minutes=6)))
        self.assertEqual(matured, 1)
        self.assertEqual(setup["outcomes"]["5"]["status"], "SL_AMBIGUO")


if __name__ == "__main__":
    unittest.main()
