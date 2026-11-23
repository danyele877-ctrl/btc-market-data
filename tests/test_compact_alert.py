import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from render_bridge_publication import BTC_ID, ZEC_ID, ZEC_SPOT_ID, build_compact_alert


class CompactAlertTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "data" / "radar_snapshot.json"
        self.snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.now = datetime.fromisoformat(self.snapshot["generated_at"])

    def test_real_snapshot_projection_and_expiry(self):
        alert = build_compact_alert(self.snapshot, self.now)
        self.assertEqual(alert["generated_at"], self.snapshot["generated_at"])
        self.assertEqual(alert["captured_at"], self.snapshot["source_captured_at"])
        for instrument, field, key, source in (
            (BTC_ID, "last", "btc_spot_ticker", "last"),
            (ZEC_SPOT_ID, "last", "zec_spot_ticker", "last"),
            (ZEC_ID, "last", "zec_xperp_ticker", "last"),
            (ZEC_ID, "mark", "zec_xperp_mark", "markPx"),
        ):
            first = self.snapshot["market"][key]["first"]
            self.assertEqual(alert["prices"][instrument][field], float(first[source]))
        self.assertLess(len(json.dumps(alert)), 2048)
        expired = build_compact_alert(self.snapshot, self.now + timedelta(minutes=10))
        self.assertEqual(expired["decision"], {"side": "NO_SIGNAL", "reason": "stale_data"})

    def test_confirmed_direction_must_still_pass_quality_time_and_invalidation_checks(self):
        early = self.snapshot["zec_early_signal"]
        zec = float(self.snapshot["market"]["zec_xperp_ticker"]["first"]["last"])
        early["data_quality"].update(websocket_ready=True, required_groups_ready=True, blockers=[])
        for direction, valid, crossed in (("LONG", zec / 2, zec), ("SHORT", zec * 2, zec)):
            with self.subTest(direction=direction):
                early.update(
                    decision=direction, candidate_direction=direction, status="TRIGGER CONFERMATO",
                    captured_at=self.snapshot["source_captured_at"],
                    valid_until=(self.now + timedelta(seconds=15)).isoformat(),
                    invalidation={"direction": direction, "price": valid},
                    confirmation_5m={"close_price": zec,
                                     "closed_at": self.snapshot["source_captured_at"],
                                     "breakout_reference": valid},
                )
                alert = build_compact_alert(self.snapshot, self.now)
                self.assertEqual(alert["decision"]["side"], direction)
                self.assertEqual(alert["decision"]["confirmation_5m"]["close_price"], zec)
                self.assertEqual(alert["decision"]["confirmation_5m"]["breakout_reference"], valid)
                early["confirmation_5m"]["closed_at"] = (self.now + timedelta(minutes=1)).isoformat()
                self.assertNotIn("confirmation_5m", build_compact_alert(self.snapshot, self.now)["decision"])
                self.assertEqual(alert["feed"]["expires_at"], early["valid_until"])
                deadline = early.pop("valid_until")
                self.assertEqual(build_compact_alert(self.snapshot, self.now)["decision"]["side"], "NO_SIGNAL")
                early["valid_until"] = deadline
                early["invalidation"]["price"] = crossed
                self.assertEqual(build_compact_alert(self.snapshot, self.now)["decision"]["side"], "NO_SIGNAL")
                early["invalidation"]["price"] = valid
                early["status"] = "PRE-ALLERTA"
                self.assertEqual(build_compact_alert(self.snapshot, self.now)["decision"]["side"], "NO_SIGNAL")
                early["status"] = "TRIGGER CONFERMATO"
                when_expired = datetime.fromisoformat(build_compact_alert(self.snapshot, self.now)["feed"]["expires_at"])
                self.assertEqual(build_compact_alert(self.snapshot, when_expired)["decision"]["side"], "NO_SIGNAL")
                early["captured_at"] = None
                self.assertEqual(build_compact_alert(self.snapshot, self.now)["decision"]["side"], "NO_SIGNAL")
        early["captured_at"] = self.snapshot["source_captured_at"]
        early["data_quality"]["websocket_ready"] = False
        self.assertEqual(build_compact_alert(self.snapshot, self.now)["decision"]["side"], "NO_SIGNAL")

    def test_malformed_or_future_timestamps_fail_closed(self):
        for stamp in ("unparseable", "2099-01-01T00:00:00Z", None):
            with self.subTest(stamp=stamp):
                self.snapshot["source_captured_at"] = stamp
                alert = build_compact_alert(self.snapshot, self.now)
                self.assertIsNone(alert["feed"]["expires_at"])
                self.assertEqual(alert["decision"], {"side": "NO_SIGNAL", "reason": "invalid_timestamps"})
