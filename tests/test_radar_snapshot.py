import unittest
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from build_radar_snapshot import summarize_history
from capture_render_feed import get_okx_xperp_history


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
