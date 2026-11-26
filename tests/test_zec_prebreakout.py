import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from telegram_alert import TelegramAlert
from zec_limit_tracking import live_prebreakout
from zec_prebreakout_tracking import outcome, update_notices


class PrebreakoutTests(unittest.TestCase):
    def test_long_and_short_require_an_intact_level_and_continuous_tape(self):
        now = datetime(2026, 11, 26, 21, 2, tzinfo=timezone.utc)
        start = now - timedelta(minutes=2)
        for side, sign in (("LONG", 1), ("SHORT", -1)):
            with self.subTest(side=side):
                closed = [{"completed_at": (start - timedelta(minutes=5 * n)).isoformat(),
                           "high": 804, "low": 796, "close": 800} for n in range(7, -1, -1)]
                minutes = [{"completed_at": (now - timedelta(minutes=5)).isoformat(), "close": 800}]
                events = [{"ts_ms": int(now.timestamp() - age) * 1000, "trade_id": trade_id,
                           "price": 800 + sign * step, "delta_contracts": sign * contracts}
                          for trade_id, age, step, contracts in ((1, 45, 2, 1), (2, 2, 3, 5))]
                health = {"connected": True, "book_valid": True, "book_gap_count": 0,
                          "trade_events_truncated": False, "book_age_seconds": 1, "trade_age_seconds": 2,
                          "cvd": {"ready_1m": True, "gap_count_1m": 0, "1m": sign * 6, "ready_5m": True},
                          "book": {"imbalance_top10": sign * 0.4}, "trade_events": events,
                          "book_history": [{"ts_ms": int(now.timestamp() - 20) * 1000,
                                            "imbalance_top10": sign * 0.1}]}
                result = live_prebreakout(now, closed, minutes, health)
                self.assertEqual(result["side"], side)
                self.assertEqual(result["level"], 800 + sign * 4)
                self.assertEqual(result["invalidation"], 800 - sign)
                self.assertIsNone(live_prebreakout(now, closed, minutes, {**health, "cvd": {}}))
                events[0]["price"] = result["level"]
                self.assertIsNone(live_prebreakout(now, closed, minutes, health))

    def test_accepted_notice_is_logged_once_across_instances_and_tracks_both_horizons(self):
        now = datetime(2026, 11, 26, 21, 2, 15, tzinfo=timezone.utc)
        stamp = now.timestamp()
        notice = {"observed_at": stamp, "side": "SHORT", "price": 801, "level": 800, "invalidation": 805}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fixture", "TELEGRAM_CHAT_ID": "123", "RADAR_TELEGRAM_DATABASE_URL": "",
            "RADAR_TELEGRAM_STATE_PATH": str(Path(tmp) / "delivery.db"),
        }), patch("telegram_alert.time.time", return_value=stamp + 1), patch("telegram_alert.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = '{"ok":true}'
            TelegramAlert().publish_prebreakout(notice, now)
            alert = TelegramAlert()
            alert.publish_prebreakout(notice, now)
            alert.publish_prebreakout({**notice, "observed_at": stamp + 15}, now + timedelta(seconds=15))
            send.assert_called_once()
            text = json.loads(send.call_args.args[0].data)["text"]
            self.assertIn("Supporto ancora intatto: <b>800.00 USD</b>", text)
            self.assertIn("LIMIT NON ATTIVABILE", text)
            first_close = int(stamp) // 60 * 60 + 60
            bars = {first_close + offset * 60: (801, 804) for offset in range(16)}
            bars[first_close + 6 * 60] = (799, 804)
            with alert.connection() as (db, param):
                [record] = update_notices(db, param, bars)
            self.assertEqual(record["telegram_accepted_at"], stamp + 1)
            self.assertEqual(record["outcomes"]["5m"]["status"], "no_touch")
            self.assertEqual(record["outcomes"]["15m"], {"status": "breakout", "elapsed_s_bounds": [344, 404]})

    def test_partial_candles_and_missing_data_cannot_prove_a_breakout(self):
        notice = {"side": "LONG", "level": 804, "invalidation": 799}
        self.assertEqual(outcome(notice, 10, 5, {60: (801, 804)})["status"], "ambiguous")
        self.assertEqual(outcome(notice, 10, 5, {120: (801, 804)})["status"], "missing_data")
        self.assertEqual(outcome(notice, 10, 5, {60: (800, 803), 120: (799, 804)})["status"], "ambiguous")
        self.assertEqual(outcome(notice, 10, 5, {60: (800, 803), 120: (799, 803)})["status"], "invalidation")
