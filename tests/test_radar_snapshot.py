import unittest
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from build_radar_snapshot import microstructure_summary, summarize_history, funding_diagnostics
from capture_render_feed import get_okx_xperp_history, validate_feed


NOW = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


def ready_record(http_status):
    end_ms = int(NOW.timestamp() * 1000)
    candles = []
    for index in range(200):
        ts_ms = end_ms - (200 - index) * 60 * 60 * 1000
        completed = datetime.fromtimestamp((ts_ms + 60 * 60 * 1000) / 1000, timezone.utc)
        candles.append({
            "ts_ms": ts_ms,
            "completed_at": completed.isoformat().replace("+00:00", "Z"),
            "open": "100", "high": "101", "low": "99", "close": "100",
        })
    return {"http_status": http_status, "candles": candles}


class RadarHistoryValidationTests(unittest.TestCase):
    def valid_feed(self):
        now = datetime.now(timezone.utc)
        captured_at = now.isoformat().replace("+00:00", "Z")
        instruments = {}
        streams = {}
        active = [
            {"instType": "SPOT", "instId": "BTC-USDC", "stream_key": "BTC-USDC"},
            {"instType": "SPOT", "instId": "ZEC-USDC", "stream_key": "ZEC-USDC"},
            {"instType": "FUTURES", "instId": "ZEC-USD_UM_XPERP-310530", "stream_key": "ZEC-USD_UM_XPERP"},
        ]
        for market, stream_key in (
            ("BTC-USDC", "BTC-USDC"), ("ZEC-USDC", "ZEC-USDC"),
            ("ZEC-USD_UM_XPERP", "ZEC-USD_UM_XPERP"),
        ):
            instruments[market] = {}
            for bar, seconds in (("1m", 60), ("5m", 300)):
                interval_ms = seconds * 1000
                end = int(now.timestamp()) // seconds * seconds
                latest_start = (end - seconds) * 1000
                rows = []
                for index in range(200):
                    ts_ms = latest_start - (199 - index) * interval_ms
                    completed = datetime.fromtimestamp((ts_ms + interval_ms) / 1000, timezone.utc)
                    rows.append({
                        "ts_ms": ts_ms,
                        "timestamp": datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                        "completed_at": completed.isoformat().replace("+00:00", "Z"),
                        "open": "100", "high": "101", "low": "99", "close": "100",
                    })
                instruments[market][bar] = rows
                streams[f"{stream_key}:{bar}"] = {
                    "candle_count": 200, "history_ready_200": True, "gap_count": 0,
                    "stale": False, "last_completed_at": rows[-1]["completed_at"],
                    "last_candle_at": rows[-1]["timestamp"], "age_seconds": 1,
                }
        feed = {
            "captured_at": captured_at, "status": "ok",
            "healthz": {"http_status": 200, "payload": {
                "status": "ok", "websocket_connected": True, "last_message_age_seconds": 1,
                "active_instruments": active, "streams": streams,
                "bridge_dispatch_enabled": True, "bridge_dispatch_last_status": 204,
                "bridge_dispatch_last_error": None,
                "microstructure": {"connected": True, "book_valid": True,
                    "message_age_seconds": 1, "trade_age_seconds": 1, "book_age_seconds": 1},
            }},
            "candles": {"http_status": 200, "payload": {"instruments": instruments}},
            "okx_history": {},
            "zec_xperp_history": {"instrument": "ZEC-USD_UM_XPERP-310530", "retrieved_at": captured_at, "bars": {}},
        }
        for market in ("BTC-USDC", "ZEC-USDC"):
            feed["okx_history"][market] = {}
            for bar, seconds in (("1H", 3600), ("4H", 14400)):
                end = int(now.timestamp()) // seconds * seconds
                candles = [
                    {
                        "ts_ms": (end - (200 - index) * seconds) * 1000,
                        "completed_at": datetime.fromtimestamp(end - (199 - index) * seconds, timezone.utc).isoformat().replace("+00:00", "Z"),
                        "open": "100", "high": "101", "low": "99", "close": "100",
                    }
                    for index in range(200)
                ]
                feed["okx_history"][market][bar] = {"http_status": 200, "count": 200, "candles": candles}
        for bar, seconds in (("1m", 60), ("5m", 300), ("1H", 3600), ("4H", 14400)):
            end = int(now.timestamp()) // seconds * seconds
            newest = datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z")
            feed["zec_xperp_history"]["bars"][bar] = {
                "http_status": 200, "count": 200, "gap_count": 0, "ready_200": True,
                "newest_completed_at": newest, "error": None,
            }
        return feed

    def test_rest_trade_polling_gaps_do_not_contaminate_live_ws_gap_status(self):
        result = microstructure_summary({
            "book_snapshots": [
                {"source": "rest", "ts_ms": 1000},
                {"source": "websocket", "ts_ms": 1000, "seq_id": 10, "prev_seq_id": -1},
                {"source": "websocket", "ts_ms": 1000, "seq_id": 11, "prev_seq_id": 10},
            ],
            "trades": [
                {"source": "rest", "tradeId": "10", "ts_ms": 1000},
                {"source": "rest", "tradeId": "100", "ts_ms": 2000},
                {"source": "websocket", "tradeId": "101", "ts_ms": 2000, "gap_before": False},
                {"source": "websocket", "tradeId": "105", "ts_ms": 3000, "gap_before": True},
            ],
        })
        self.assertEqual(result["rest_trade_count"], 2)
        self.assertEqual(result["websocket_trade_count"], 2)
        self.assertIsNone(result["rest_trade_gap_events"])
        self.assertEqual(result["websocket_trade_gap_events"], 1)
        self.assertEqual(result["trade_id_gap_events"], 1)
        self.assertIn("campionato", result["rest_history_cvd_status"])
        self.assertEqual(result["book_snapshot_count"], 3)
        self.assertEqual(result["book_duplicate_timestamp_collisions"], 2)
        self.assertEqual(result["book_cross_source_timestamp_groups"], 1)
        self.assertEqual(result["book_same_source_timestamp_groups"], 1)

    def test_feed_validation_accepts_fresh_spot_history(self):
        self.assertEqual(validate_feed(self.valid_feed()), [])

    def test_feed_validation_rejects_empty_and_stale_history(self):
        feed = self.valid_feed()
        feed["okx_history"]["BTC-USDC"]["1H"]["candles"] = []
        feed["captured_at"] = "2000-01-01T00:00:00Z"
        problems = validate_feed(feed)
        self.assertTrue(any("captured_at" in problem for problem in problems))
        self.assertTrue(any("BTC-USDC 1H" in problem for problem in problems))

    def test_feed_validation_rejects_gapped_spot_history(self):
        feed = self.valid_feed()
        feed["okx_history"]["ZEC-USDC"]["4H"]["candles"][20]["ts_ms"] += 1
        self.assertTrue(any("ZEC-USDC 4H" in problem for problem in validate_feed(feed)))

    def test_feed_validation_rejects_impossible_spot_ohlc(self):
        feed = self.valid_feed()
        feed["okx_history"]["BTC-USDC"]["1H"]["candles"][10]["high"] = "98"
        self.assertTrue(any("BTC-USDC 1H" in problem for problem in validate_feed(feed)))

    def test_xperp_history_records_only_successful_http_responses(self):
        now_ms = int(time.time() * 1000)
        rows = [
            [str(now_ms - (200 - index) * 60_000), "100", "101", "99", "100", "1", "1", "100", "1"]
            for index in range(200)
        ]
        for status in (200, 201):
            with self.subTest(status=status):
                response = MagicMock()
                response.__enter__.return_value.status = status
                response.__enter__.return_value.read.return_value = json.dumps({"code": "0", "data": rows}).encode()
                with patch("capture_render_feed.urllib.request.urlopen", return_value=response):
                    item = get_okx_xperp_history("ZEC-USD_UM_XPERP-310530", bars=("1m",), target=200)["bars"]["1m"]
                self.assertEqual(item["http_status"], status)
                self.assertEqual(item["ready_200"], status == 200)

    def test_history_requires_explicit_http_200(self):
        for status in (None, 201, 404):
            with self.subTest(status=status):
                summary = summarize_history(ready_record(status), "1H", NOW)
                self.assertFalse(summary["ready_200"])
                self.assertIsNone(summary["indicators"])

    def test_history_accepts_exact_http_200(self):
        summary = summarize_history(ready_record(200), "1H", NOW)
        self.assertTrue(summary["ready_200"])
        self.assertIsNotNone(summary["indicators"])

    def test_funding_diagnostics_keep_four_hour_spec_separate_from_eight_hour_api_fields(self):
        base = 1_800_000_000_000
        record = {"http_status": 200, "api_code": "0", "exchange_age_seconds": 1,
            "first": {"instId": "ZEC-USD_UM_XPERP-310530", "prevFundingTime": str(base),
                "fundingTime": str(base + 8 * 3_600_000),
                "nextFundingTime": str(base + 16 * 3_600_000),
                "fundingRate": "0.0001", "settState": "settled"}}
        result = funding_diagnostics(record, NOW)
        self.assertEqual(result["published_reference_interval_hours"], 4)
        self.assertEqual(result["prev_to_funding_hours_observed"], 8)
        self.assertEqual(result["funding_to_next_hours_observed"], 8)
        self.assertEqual(result["status"], "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H")
        self.assertEqual(result["live_next_interval_hours"], 8.0)
        self.assertIn("nextFundingTime", result["interpretation"])


if __name__ == "__main__":
    unittest.main()
