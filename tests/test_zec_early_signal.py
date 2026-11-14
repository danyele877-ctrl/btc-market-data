import unittest
from datetime import datetime, timezone

from zec_early_signal import INSTRUMENT_ID, evaluate_signal


CAPTURED = "2026-01-01T12:10:00Z"
NOW = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


def candles(bar, base_price, interval_minutes, count=8, drift=1.0):
    interval_ms = interval_minutes * 60_000
    end_ms = int(NOW.timestamp() * 1000)
    latest_start = end_ms - interval_ms
    rows = []
    for index in range(count):
        price = base_price + index * drift
        start = latest_start - (count - index - 1) * interval_ms
        rows.append({
            "ts_ms": start,
            "completed_at": datetime.fromtimestamp((start + interval_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "open": price - 0.2,
            "high": price + 0.3,
            "low": price - 0.5,
            "close": price,
        })
    return rows


def market_record(inst_id, fields=None):
    return {
        "http_status": 200,
        "api_code": "0",
        "exchange_age_seconds": 1,
        "first": {"instId": inst_id, **(fields or {})},
    }


def valid_snapshot():
    tail = {}
    for instrument, price in (("ZEC-USD_UM_XPERP", 100), ("ZEC-USDC", 100), ("BTC-USDC", 1000)):
        tail[instrument] = {
            "1m": {"tail": candles("1m", price, 1)},
            "5m": {"tail": candles("5m", price, 5)},
        }
    return {
        "schema_version": 3,
        "source_captured_at": CAPTURED,
        "source_status": "ok",
        "render_health_http": 200,
        "render_candles_http": 200,
        "render_health": {"status": "ok", "websocket_connected": True},
        "read_errors": {},
        "market": {
            "zec_xperp_ticker": market_record(INSTRUMENT_ID),
            "zec_xperp_instrument": market_record(INSTRUMENT_ID, {"ctVal": "0.01"}),
            "zec_spot_ticker": market_record("ZEC-USDC"),
            "btc_spot_ticker": market_record("BTC-USDC"),
        },
        "candles_1m_5m": tail,
        "microstructure": {
            "websocket": {
                "connected": True, "book_valid": True,
                "message_age_seconds": 1, "trade_age_seconds": 1, "book_age_seconds": 1,
            },
            "cvd": {"ready_1m": True, "ready_5m": True, "1m": 10, "5m": 20},
            "book_imbalance": {"imbalance_top10": 0.8},
            "open_interest_history": [
                {"ts_ms": int(NOW.timestamp() * 1000) - 120_000, "oi_contracts": "100", "price_usd": "100", "inst_id": INSTRUMENT_ID},
                {"ts_ms": int(NOW.timestamp() * 1000), "oi_contracts": "101", "price_usd": "101", "inst_id": INSTRUMENT_ID},
            ],
        },
    }


class ZecEarlySignalTests(unittest.TestCase):
    def test_missing_snapshot_waits_and_has_no_directional_decision(self):
        result = evaluate_signal({}, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")
        self.assertFalse(result["execution"]["orders_enabled"])

    def test_exact_xperp_contract_is_required(self):
        snapshot = valid_snapshot()
        snapshot["market"]["zec_xperp_ticker"]["first"]["instId"] = "ZEC-USDT-SWAP"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertIn(INSTRUMENT_ID, " ".join(result["data_quality"]["blockers"]))

    def test_flow_and_book_are_one_correlated_score_group(self):
        result = evaluate_signal(valid_snapshot(), now=NOW)
        self.assertEqual(result["scores"]["groups"]["order_flow"]["weight"], 0.35)
        self.assertNotIn("cvd", result["scores"]["groups"])
        self.assertNotIn("book", result["scores"]["groups"])

    def test_valid_strong_setup_is_simulation_only_and_never_sizes(self):
        result = evaluate_signal(valid_snapshot(), now=NOW)
        self.assertEqual(result["status"], "TRIGGER CONFERMATO")
        self.assertEqual(result["decision"], "LONG")
        self.assertEqual(result["instrument"]["inst_id"], INSTRUMENT_ID)
        self.assertEqual(result["contract_safety"]["ctVal_status"], "NON RISOLTO")
        self.assertFalse(result["contract_safety"]["sizing_allowed"])
        self.assertIsNone(result["execution"]["position_quantity"])
        self.assertIsNone(result["execution"]["margin"])
        self.assertIsNone(result["execution"]["pnl"])

    def test_stale_snapshot_waits(self):
        snapshot = valid_snapshot()
        snapshot["source_captured_at"] = "2026-01-01T12:00:00Z"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")

    def test_unresolved_oi_reduces_confidence_but_is_optional(self):
        snapshot = valid_snapshot()
        snapshot["microstructure"]["open_interest_history"] = []
        result = evaluate_signal(snapshot, now=NOW)
        self.assertNotIn("positioning", result["scores"]["groups"])
        self.assertTrue(result["data_quality"]["limitations"])
        self.assertLessEqual(result["confidence"]["score"], 65)

    def test_unclosed_candle_blocks_signal(self):
        snapshot = valid_snapshot()
        snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["1m"]["tail"][-1]["completed_at"] = "2026-01-01T12:11:00Z"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")


if __name__ == "__main__":
    unittest.main()
