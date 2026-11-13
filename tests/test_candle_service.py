import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import candle_service as service


class CandleParsingTests(unittest.TestCase):
    def test_only_completed_candles_are_accepted(self):
        row = ["1794570000000", "100", "105", "99", "103", "12", "12", "1236", "0"]
        self.assertIsNone(service.parse_candle(row, "candle1m"))
        row[8] = "1"
        candle = service.parse_candle(row, "candle1m")
        self.assertEqual(candle["open"], "100")
        self.assertEqual(candle["close"], "103")
        start = datetime.fromtimestamp(1794570000, timezone.utc)
        expected_close = start.timestamp() + 60
        self.assertEqual(candle["timestamp"], start.isoformat().replace("+00:00", "Z"))
        self.assertEqual(
            candle["completed_at"],
            datetime.fromtimestamp(expected_close, timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def test_malformed_rows_and_unknown_bars_are_ignored(self):
        self.assertIsNone(service.parse_candle([], "candle1m"))
        self.assertIsNone(service.parse_candle(["1"] * 9, "candle15m"))

    def test_candles_are_merged_in_time_order_without_duplicates(self):
        target = []
        first = {"ts_ms": 2000, "close": "2"}
        second = {"ts_ms": 1000, "close": "1"}
        replacement = {"ts_ms": 2000, "close": "3"}
        service.merge_candles(target, [first, second])
        service.merge_candles(target, [replacement])
        self.assertEqual([item["ts_ms"] for item in target], [1000, 2000])
        self.assertEqual(target[-1]["close"], "3")


class InstrumentDiscoveryTests(unittest.TestCase):
    def test_uses_okx_eea_endpoints(self):
        self.assertEqual(service.OKX_REST, "https://eea.okx.com")
        self.assertEqual(service.OKX_WS, "wss://wseea.okx.com/ws/v5/business")

    def test_metadata_selects_live_xperp_id_and_only_available_spot_pairs(self):
        spot = [
            {"instId": "BTC-USDC", "state": "live"},
            {"instId": "ZEC-USDC", "state": "suspend"},
        ]
        futures = [
            {"instId": "ZEC-USD_UM_XPERP", "state": "live"},
            {"instId": "ZEC-USDT-SWAP", "state": "live"},
        ]
        active, unavailable = service.resolve_instruments(spot, futures)
        self.assertEqual(
            active,
            [
                {"instType": "SPOT", "instId": "BTC-USDC"},
                {"instType": "FUTURES", "instId": "ZEC-USD_UM_XPERP"},
            ],
        )
        self.assertEqual(unavailable, ["ZEC-USDC"])


class FeedStateTests(unittest.TestCase):
    def test_websocket_messages_store_only_closed_candles(self):
        async def run_case():
            with TemporaryDirectory() as directory:
                feed = service.CandleFeed(Path(directory) / "candles.json")
                base = ["1794570000000", "100", "105", "99", "103", "12", "12", "1236", "0"]
                open_message = {
                    "arg": {"instId": "BTC-USDC", "channel": "candle1m"},
                    "data": [base],
                }
                await feed.handle_message(json.dumps(open_message))
                self.assertEqual(feed.state["instruments"]["BTC-USDC"]["1m"], [])
                self.assertIsNotNone(feed.state["service"]["last_message_at"])

                base[8] = "1"
                closed_message = dict(open_message, data=[base])
                await feed.handle_message(json.dumps(closed_message))
                candles = feed.state["instruments"]["BTC-USDC"]["1m"]
                self.assertEqual(len(candles), 1)
                self.assertTrue((Path(directory) / "candles.json").exists())

        asyncio.run(run_case())

    def test_health_reports_missing_streams_stale(self):
        feed = service.CandleFeed()
        health = feed.health()
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["streams"]["BTC-USDC:1m"]["stale"])

    def test_http_health_route_serves_json(self):
        async def run_case():
            feed = service.CandleFeed()

            async def handle(reader, writer):
                await service.handle_http(feed, reader, writer)

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
                self.assertIn(b"HTTP/1.1 200 OK", response)
                self.assertIn(b'"status": "degraded"', response)
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
