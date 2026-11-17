import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import capture_render_feed as capture


class MicrostructureHistoryTests(unittest.TestCase):
    def test_generic_json_writer_replaces_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "history.json"
            target.write_text('{"old":true}', encoding="utf-8")
            capture.write_json_atomically(target, {"new": True, "items": [1, 2, 3]})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True, "items": [1, 2, 3]})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_websocket_trade_and_depth_sequences_are_persisted(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        micro = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "trade_events": [{
                "trade_id": 123, "ts_ms": now_ms-1000, "side": "sell",
                "size_contracts": 4.5, "price": "900.1", "count": 1,
                "gap_before": False,
            }],
            "trade_events_returned": 1,
            "trade_events_truncated": False,
            "trade_gap_count": 0, "book_gap_count": 0, "book_valid": True,
            "trade_age_seconds": 1, "book_age_seconds": 1,
            "book_depth_history": [{
                "ts_ms": now_ms-500, "seq_id": 456,
                "bids": [["900.0", "10"]], "asks": [["900.2", "12"]],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "micro.json"
            with patch.object(capture, "MICRO_OUT", path):
                status = capture.update_microstructure_history({}, micro)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(status["websocket_trades"], 1)
        self.assertEqual(status["websocket_book_snapshots"], 1)
        self.assertEqual(saved["trades"][0]["source"], "websocket")
        self.assertEqual(saved["trades"][0]["gap_before"], False)
        self.assertEqual(saved["book_snapshots"][0]["seq_id"], 456)
        self.assertEqual(saved["book_snapshots"][0]["source"], "websocket")
        self.assertEqual(len(saved["capture_status"]), 1)
        self.assertTrue(saved["capture_status"][0]["book_valid"])

    def test_bridge_preserves_500_intraday_candles(self):
        rows = [{"ts_ms": i, "open": "1", "high": "1", "low": "1", "close": "1"} for i in range(600)]
        result = capture.trim_arrays({"candles": rows})
        self.assertEqual(len(result["candles"]), 500)
        self.assertEqual(result["candles"][0]["ts_ms"], 100)
        self.assertEqual(result["candles"][-1]["ts_ms"], 599)

    def test_rest_duplicate_is_upgraded_to_websocket_record(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "micro.json"
            path.write_text(json.dumps({
                "schema_version": 1, "instrument": "ZEC-USD_UM_XPERP-310530",
                "book_snapshots": [], "trades": [{
                    "tradeId": "123", "ts_ms": now_ms-1000, "side": "sell",
                    "sz": "4.5", "px": "900.1", "source": "rest",
                }], "open_interest_history": [],
            }), encoding="utf-8")
            micro = {"trade_events": [{
                "trade_id": 123, "ts_ms": now_ms-1000, "side": "sell",
                "size_contracts": 4.5, "price": "900.1", "count": 1,
                "gap_before": True,
            }]}
            with patch.object(capture, "MICRO_OUT", path):
                status = capture.update_microstructure_history({}, micro)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(status["unique_trades"], 1)
        self.assertEqual(saved["trades"][0]["source"], "websocket")
        self.assertTrue(saved["trades"][0]["gap_before"])


if __name__ == "__main__":
    unittest.main()
