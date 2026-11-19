import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import capture_render_feed as capture


class CandleAlignmentTests(unittest.TestCase):
    def test_candle_refresh_reuses_only_contiguous_history_overlapping_rest_page(self):
        captured = 1_791_500_000_000 // 60_000 * 60_000
        previous = [
            {
                "ts_ms": captured - age * 60_000, "close": "3",
                "completed_at": datetime.fromtimestamp(captured / 1000 - (age - 1) * 60, timezone.utc).isoformat(),
            }
            for age in range(210, 10, -1)
        ]
        rest = [
            [str(captured - age * 60_000), "4", "4", "4", "4", "1", "1", "1", "1"]
            for age in range(1, 101)
        ]
        with patch("capture_render_feed.urllib.request.urlopen") as request:
            response = request.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps({"code": "0", "data": rest}).encode()
            record = capture.get_okx_xperp_history(
                "BTC-USDC", bars=("1m",), target=200, previous={"1m": previous},
            )["bars"]["1m"]
        request.assert_called_once()
        self.assertTrue(record["ready_200"])
        self.assertEqual(record["candles"][0]["close"], "3")
        self.assertEqual(record["candles"][-1]["close"], "4")

    def test_adjacent_last_closes_align_without_hiding_larger_delays(self):
        for lag in (1, 2):
            with self.subTest(lag=lag):
                histories = {}
                for market, end in (("btc", 500), ("spot", 500-lag), ("xperp", 500)):
                    histories[market] = {"bars": {}}
                    for bar, interval in (("1m", 60_000), ("5m", 300_000)):
                        candles = [
                            {"ts_ms": step * interval, "completed_at": str(step + 1)}
                            for step in range(end - 499, end + 1)
                        ]
                        histories[market]["bars"][bar] = {
                            "candles": candles, "count": 500, "ready_200": True,
                            "newest_completed_at": candles[-1]["completed_at"],
                        }
                capture.align_intraday_histories(histories)
                for bar in ("1m", "5m"):
                    record = histories["btc"]["bars"][bar]
                    spot = histories["spot"]["bars"][bar]
                    self.assertEqual(record["count"], 499 if lag == 1 else 500)
                    self.assertEqual(record["newest_completed_at"] == spot["newest_completed_at"], lag == 1)


class CandleRolloverTests(unittest.TestCase):
    def test_retry_targets_next_real_close_without_extending_candle_freshness(self):
        streams = {
            f"{market}:5m": {"last_completed_at": "2026-11-19T19:25:00+00:00"}
            for market in capture.HISTORY_MARKETS
        }
        output = {"healthz": {"payload": {"streams": streams}}}
        for current, expected in (("19:28:00", None), ("19:28:20", 105), ("19:30:02", 3)):
            reference = datetime.fromisoformat(f"2026-11-19T{current}+00:00")
            self.assertEqual(capture.next_5m_retry_delay(output, reference), expected)


class WebSocketWindowTests(unittest.TestCase):
    def test_hybrid_requires_fresh_complete_sequence(self):
        micro = {
            "connected": True, "book_valid": True, "book_gap_count": 0,
            "trade_events_truncated": False,
            "message_age_seconds": 0, "book_age_seconds": 0.5, "trade_age_seconds": 2,
            "cvd": {"ready_1m": True, "ready_5m": True, "gap_count_1m": 0, "gap_count_5m": 0,
                    "trade_count_5m": 25, "1m": -10, "5m": 30},
        }
        self.assertTrue(capture.websocket_window_ready(micro))
        for field, bad in (("book_gap_count", 1), ("trade_events_truncated", True),
                           ("trade_age_seconds", 181), ("book_age_seconds", -0.1),
                           ("cvd", {**micro["cvd"], "gap_count_5m": 1})):
            with self.subTest(field=field):
                self.assertFalse(capture.websocket_window_ready({**micro, field: bad}))


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

    def test_book_keeps_rest_websocket_collision_and_same_ms_ws_sequences(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        depth = {"bids": [["900", "1"]], "asks": [["901", "1"]]}
        micro = {"book_depth_history": [
            {"ts_ms": now_ms - 1000, "seq_id": 10, "prev_seq_id": -1, **depth},
            {"ts_ms": now_ms - 1000, "seq_id": 11, "prev_seq_id": 10, **depth},
        ]}
        rest = {"zec_xperp_book": {"http_status": 200, "retrieved_at": "2026-01-01T00:00:00Z",
            "payload": {"data": [{"ts": str(now_ms - 1000), **depth}]}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "micro.json"
            with patch.object(capture, "MICRO_OUT", path):
                capture.update_microstructure_history(rest, micro)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["book_snapshots"]), 3)
        self.assertEqual({row["source"] for row in saved["book_snapshots"]}, {"rest", "websocket"})
        ws_rows = [row for row in saved["book_snapshots"] if row["source"] == "websocket"]
        self.assertEqual({row["seq_id"] for row in ws_rows}, {10, 11})
        self.assertEqual({row["prev_seq_id"] for row in ws_rows}, {-1, 10})


if __name__ == "__main__":
    unittest.main()
