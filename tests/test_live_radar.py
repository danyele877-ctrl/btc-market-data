import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from live_radar import LiveRadar


class LiveRadarTests(unittest.TestCase):
    def test_pending_never_emits_a_signal(self):
        self.assertEqual(LiveRadar(Mock()).current()["decision"]["side"], "NO_SIGNAL")

    def test_live_disconnect_blocks_cached_decision(self):
        radar = LiveRadar(Mock())
        radar.snapshot = {"decision": {"side": "LONG"}}
        with patch("live_radar.websocket_window_ready", return_value=False):
            result = radar.current()
        self.assertFalse(result["quality"]["engine_ready"])
        self.assertEqual(result["decision"]["side"], "NO_SIGNAL")

    def test_rebuilds_compact_result_at_read_time(self):
        radar = LiveRadar(Mock())
        radar.snapshot = {}
        with patch("live_radar.websocket_window_ready", return_value=True), patch(
            "live_radar.build_compact_alert", return_value={"decision": {"side": "NO_SIGNAL"}}
        ) as build:
            radar.current()
        self.assertIs(build.call_args.args[0], radar.snapshot)
        self.assertIsNotNone(build.call_args.args[1].tzinfo)

    def test_collector_history_uses_only_contiguous_suffix_with_minimum_coverage(self):
        from capture_render_feed import collector_histories

        rows = [{"ts_ms": i * 60_000, "completed_at": str(i)} for i in range(211) if i != 10]
        record = collector_histories({"BTC-USDC": {"1m": rows}})["BTC-USDC"]["bars"]["1m"]
        self.assertTrue(record["ready_200"])
        self.assertEqual(record["count"], 200)
        self.assertEqual(record["gap_count"], 0)

        broken = rows[:-1]
        record = collector_histories({"BTC-USDC": {"1m": broken}})["BTC-USDC"]["bars"]["1m"]
        self.assertFalse(record["ready_200"])


class LiveRadarRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_quality_failure_until_engine_ready_in_same_slot(self):
        feed = Mock()
        feed.state = {"instruments": {}}
        feed.health.return_value = {"websocket_connected": True}
        radar = LiveRadar(feed)
        reference = datetime(2026, 11, 19, 23, 10, 30, tzinfo=timezone.utc)
        snapshot = {
            "candles_1m_5m": {
                "ZEC-USD_UM_XPERP": {
                    "5m": {"latest": {"completed_at": "2026-11-19T23:10:00Z"}}
                }
            }
        }
        output = {"healthz": {"payload": {}}, "candles": {"payload": {}}}
        blocked = {"quality": {"engine_ready": False}}
        ready = {"quality": {"engine_ready": True}, "decision": {"side": "NO_SIGNAL"}}
        with (
            patch("live_radar.datetime") as clock,
            patch("live_radar.websocket_window_ready", return_value=True),
            patch("live_radar.collect_rest_snapshot", return_value=(output, {})) as collect,
            patch("live_radar.build_snapshot", return_value=snapshot),
            patch("live_radar.build_compact_alert", side_effect=[blocked, ready]),
        ):
            clock.now.return_value = reference
            await radar.refresh()
            self.assertIsNone(radar.completed_slot)
            await radar.refresh()
            self.assertEqual(radar.completed_slot, int(reference.timestamp()) // 300)
            await radar.refresh()
        self.assertEqual(collect.call_count, 2)
