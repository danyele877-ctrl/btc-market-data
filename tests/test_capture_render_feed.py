import json
import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import capture_render_feed as capture


class MicrostructureHistoryTests(unittest.TestCase):
    def _candle_payloads(self):
        previous = {
            "schema_version": 1, "generated_at": "2026-01-01T00:00:00Z",
            "service": {"status": "ok"}, "microstructure": {"connected": True}, "instruments": {},
        }
        delta = {
            "schema_version": 1, "generated_at": "2026-01-01T00:01:00Z",
            "service": {"status": "ok", "last_message_at": "2026-01-01T00:01:00Z"},
            "microstructure": {"connected": True, "last_trade_id": 201}, "instruments": {},
        }
        for instrument in capture.BRIDGE_INSTRUMENTS:
            previous["instruments"][instrument] = {}
            delta["instruments"][instrument] = {}
            for bar, interval_ms in capture.CANDLE_INTERVAL_MS.items():
                rows = [{"ts_ms": interval_ms * index, "close": str(index)} for index in range(1, 201)]
                previous["instruments"][instrument][bar] = rows
                delta["instruments"][instrument][bar] = [{
                    "ts_ms": rows[-1]["ts_ms"] + interval_ms, "close": "201"
                }]
        return previous, delta

    def test_delta_merge_keeps_complete_500_bar_shape_and_appends_only_new_rows(self):
        previous, delta = self._candle_payloads()
        result = capture.merge_candle_delta(previous, delta)
        self.assertIsNotNone(result)
        merged, added = result
        self.assertEqual(added, 15)
        rows = merged["instruments"]["ZEC-USD_UM_XPERP"]["15m"]
        self.assertEqual(len(rows), 201)
        self.assertEqual(rows[-1]["close"], "201")
        self.assertEqual(merged["generated_at"], delta["generated_at"])
        self.assertEqual(merged["service"], delta["service"])
        self.assertEqual(merged["microstructure"], delta["microstructure"])

    def test_delta_merge_rejects_gaps_and_malformed_streams_for_full_resync(self):
        previous, delta = self._candle_payloads()
        delta["instruments"]["BTC-USDC"]["1m"][0]["ts_ms"] += capture.CANDLE_INTERVAL_MS["1m"]
        self.assertIsNone(capture.merge_candle_delta(previous, delta))
        previous, delta = self._candle_payloads()
        delta["instruments"]["BTC-USDC"].pop("1m")
        self.assertIsNone(capture.merge_candle_delta(previous, delta))

    def test_incremental_fetch_uses_delta_without_full_feed_request(self):
        previous, delta = self._candle_payloads()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render_feed.json"
            path.write_text(json.dumps({"candles": {"payload": previous}}), encoding="utf-8")
            with patch.object(capture, "OUT", path), patch.object(
                capture, "get", return_value={
                    "http_status": 200,
                    "retrieved_at": "2026-01-01T00:01:01Z",
                    "latency_ms": 12,
                    "payload": delta,
                },
            ) as fetch:
                result = capture.get_candles_incrementally()
        self.assertEqual(result["transport_mode"], "delta")
        self.assertEqual(result["delta_candles_received"], 15)
        self.assertIn("/candles.delta.json?cursor=", fetch.call_args.args[0])

    def test_missing_base_falls_back_to_single_full_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with patch.object(capture, "OUT", path), patch.object(
                capture, "get", return_value={"http_status": 200, "payload": {"instruments": {}}}
            ) as fetch:
                result = capture.get_candles_incrementally()
        self.assertEqual(result["transport_mode"], "full")
        fetch.assert_called_once_with("/candles.json")

    def test_unavailable_delta_endpoint_falls_back_to_full_feed(self):
        previous, _delta = self._candle_payloads()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render_feed.json"
            path.write_text(json.dumps({"candles": {"payload": previous}}), encoding="utf-8")
            full = {"http_status": 200, "payload": {"instruments": {}}}
            with patch.object(capture, "OUT", path), patch.object(
                capture, "get", side_effect=[{"http_status": None, "error": "HTTP 404"}, full]
            ) as fetch:
                result = capture.get_candles_incrementally()
        self.assertEqual(result["transport_mode"], "full")
        self.assertEqual(fetch.call_args_list[0].args[0].split("?", 1)[0], "/candles.delta.json")
        self.assertEqual(fetch.call_args_list[1].args[0], "/candles.json")

    def test_render_get_requests_and_decompresses_gzip(self):
        raw = gzip.compress(json.dumps({"generated_at": "2026-01-01T00:00:00Z"}).encode())
        response = MagicMock()
        response.__enter__.return_value.read.return_value = raw
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.headers.get.return_value = "gzip"
        with patch("capture_render_feed.urllib.request.urlopen", return_value=response) as urlopen:
            result = capture.get("/healthz")
        sent_request = urlopen.call_args.args[0]
        self.assertEqual(result["payload"]["generated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(sent_request.get_header("Accept-encoding"), "gzip")

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
