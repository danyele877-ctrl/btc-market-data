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
