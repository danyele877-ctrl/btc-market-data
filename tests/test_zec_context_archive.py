import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zec_context_archive import INSTRUMENT_ID, _closed_1m, _mature_setups, update_context_archive


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
        for i in range(1, 6):
            start = NOW + timedelta(minutes=i)
            # On the fifth minute, both TP1=101.5 and SL=99 are touched.
            future.append(bar(start, 100, high=102 if i == 5 else 100.5,
                              low=98 if i == 5 else 99.5))
        matured = _mature_setups([setup], _closed_1m(future, NOW + timedelta(minutes=6)))
        self.assertEqual(matured, 1)
        self.assertEqual(setup["outcomes"]["5"]["status"], "SL_AMBIGUO")


if __name__ == "__main__":
    unittest.main()
