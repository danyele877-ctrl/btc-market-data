import unittest
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
