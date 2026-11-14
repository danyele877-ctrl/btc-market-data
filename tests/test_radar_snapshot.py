import unittest
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from build_radar_snapshot import summarize_history
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
        feed = {
            "captured_at": now.isoformat().replace("+00:00", "Z"),
            "status": "ok",
            "healthz": {
                "http_status": 200,
                "payload": {
                    "status": "ok", "websocket_connected": True,
                    "streams": {
                        f"{market}:{bar}": {"candle_count": 200, "stale": False}
                        for market in ("BTC-USDC", "ZEC-USDC") for bar in ("1m", "5m")
                    },
                },
            },
            "candles": {
                "http_status": 200,
                "payload": {"instruments": {market: {bar: [{"ts_ms": 1}] for bar in ("1m", "5m")} for market in ("BTC-USDC", "ZEC-USDC")}},
            },
            "okx_history": {},
        }
        for market in ("BTC-USDC", "ZEC-USDC"):
            feed["okx_history"][market] = {}
            for bar, seconds in (("1H", 3600), ("4H", 14400)):
                end = int(now.timestamp()) // seconds * seconds
                candles = [
                    {
                        "ts_ms": (end - (200 - index) * seconds) * 1000,
                        "completed_at": datetime.fromtimestamp(end - (199 - index) * seconds, timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    for index in range(200)
                ]
                feed["okx_history"][market][bar] = {"http_status": 200, "count": 200, "candles": candles}
        return feed

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


if __name__ == "__main__":
    unittest.main()
