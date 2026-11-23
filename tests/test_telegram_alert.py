import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from telegram_alert import TelegramAlert, limit_suggestion


class TelegramDeliveryTests(unittest.TestCase):
    def test_limit_uses_closed_candles_and_respects_trigger_expiry(self):
        close = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        signal = close + timedelta(seconds=4)
        expires = close + timedelta(minutes=2)
        candles = [
            {"completed_at": (close - timedelta(minutes=5 * remaining)).isoformat(),
             "low": 92, "close": 100, "high": 108}
            for remaining in range(8, -1, -1)
        ]
        for side, last, invalidation, entry, stop, target in (
            ("LONG", 100, 90, "96.00", "72.00", "120.00"),
            ("SHORT", 100, 110, "104.00", "128.00", "80.00"),
        ):
            with self.subTest(side=side):
                text = limit_suggestion(side, last, invalidation, candles, signal, expires)
                self.assertIn(f"LIMIT {side} · {entry}", text)
                self.assertIn(f"SL <b>{stop}</b> · TP1 <b>{target}</b>", text)
                self.assertIn("10:02:00 UTC", text)
        self.assertIn("LIMIT NON ATTIVABILE", limit_suggestion("LONG", 95, 90, candles, signal, expires))
        self.assertIn("LIMIT NON ATTIVABILE", limit_suggestion("LONG", 100, 90, candles[:-1], signal, expires))
        self.assertIn("LIMIT NON ATTIVABILE", limit_suggestion("LONG", 100, 97, candles, signal, expires))

    def test_deduplication_and_retry_across_instances(self):
        now = datetime.now(timezone.utc)
        alert = {
            "captured_at": now.isoformat(),
            "generated_at": now.isoformat(),
            "feed": {"expires_at": (now + timedelta(seconds=60)).isoformat()},
            "prices": {"ZEC-USD_UM_XPERP-310530": {"last": 851.2}},
            "decision": {"invalidation_price": 800},
        }
        rate_limit = HTTPError("https://api.telegram.org", 429, "", {},
                               io.BytesIO(b'{"parameters":{"retry_after":0}}'))
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fixture",
            "TELEGRAM_CHAT_ID": "123",
            "RADAR_TELEGRAM_STATE_PATH": str(Path(tmp) / "delivery.db"),
        }), patch("telegram_alert.validated_side", return_value="LONG"), patch(
            "telegram_alert.urlopen"
        ) as send:
            response = send.return_value.__enter__.return_value
            response.read.return_value = '{"ok":true}'
            send.side_effect = [rate_limit, send.return_value, TimeoutError()]
            TelegramAlert().publish(alert, 100)
            TelegramAlert().publish(alert, 100)
            TelegramAlert().publish(alert, 100)
            TelegramAlert().publish(alert, 101)
            TelegramAlert().publish(alert, 101)
            self.assertEqual(send.call_count, 3)
            payload = json.loads(send.call_args.args[0].data)
            self.assertEqual(payload["parse_mode"], "HTML")
            self.assertIn("LONG confermato · ZEC X-Perp", payload["text"])
            self.assertIn("Prezzo rilevato <b>851.20 USD</b>", payload["text"])
            self.assertIn("Invalidazione <b>800.00 USD</b>", payload["text"])
            self.assertIn("LIMIT NON ATTIVABILE", payload["text"])

    def test_macro_notice_and_fresh_direction_are_deduplicated_separately(self):
        event_at = datetime(2026, 12, 9, 19, tzinfo=timezone.utc)
        event = {"event": {"title": "Decisione FOMC", "utc": event_at.isoformat(),
                           "source": "https://www.federalreserve.gov/newsevents/calendar.htm"},
                 "pause_from": event_at.timestamp()-600, "status": "macro_pause"}
        direction = {**event, "status": "watching", "side": "SHORT", "breakout_level": 800,
                     "watch_at": event_at.timestamp()+120, "watch_until": event_at.timestamp()+1500}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fixture", "TELEGRAM_CHAT_ID": "123",
            "RADAR_TELEGRAM_STATE_PATH": str(Path(tmp) / "delivery.db"),
        }), patch("telegram_alert.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = '{"ok":true}'
            before = event_at - timedelta(minutes=10)
            TelegramAlert().publish_macro(event, now=before-timedelta(seconds=1))
            TelegramAlert().publish_macro(event, now=event_at)
            self.assertEqual(send.call_count, 0)
            TelegramAlert().publish_macro(event, now=before)
            TelegramAlert().publish_macro(event, now=before)
            self.assertEqual(send.call_count, 1)
            text = json.loads(send.call_args.args[0].data)["text"]
            self.assertIn("20:00 CET", text)
            self.assertIn("Direzione in attesa", text)
            TelegramAlert().publish_macro(direction, now=event_at+timedelta(minutes=4))
            TelegramAlert().publish_macro({**direction, "status": "breakout_reabsorbed"},
                                          now=event_at+timedelta(minutes=2))
            self.assertEqual(send.call_count, 1)
            TelegramAlert().publish_macro(direction, now=event_at+timedelta(minutes=2))
            TelegramAlert().publish_macro(direction, now=event_at+timedelta(minutes=2))
            self.assertEqual(send.call_count, 2)
            text = json.loads(send.call_args.args[0].data)["text"]
            self.assertIn("SHORT IN OSSERVAZIONE · SHADOW", text)
            self.assertIn("LIMIT non attivabile", text)

    def test_macro_reaction_waits_for_both_horizons_and_deduplicates(self):
        released_at = datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc)
        event = {"title": "Inflazione PCE e PIL USA", "source": "https://www.bea.gov/news/schedule/full"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fixture",
            "TELEGRAM_CHAT_ID": "123",
            "RADAR_TELEGRAM_STATE_PATH": str(Path(tmp) / "delivery.db"),
        }), patch("telegram_alert.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = '{"ok":true}'
            reactions = {1: (0.2, -0.1), 5: (0.5, 1.0)}
            after = released_at + timedelta(minutes=5)
            TelegramAlert().publish_macro_reaction(event, released_at, reactions, after-timedelta(seconds=1))
            TelegramAlert().publish_macro_reaction(event, released_at, {1: reactions[1]}, after)
            self.assertEqual(send.call_count, 0)
            TelegramAlert().publish_macro_reaction(event, released_at, reactions, after)
            TelegramAlert().publish_macro_reaction(event, released_at, reactions, after)
            self.assertEqual(send.call_count, 1)
            payload = json.loads(send.call_args.args[0].data)
            self.assertIn("14:30 CET", payload["text"])
            self.assertIn("1m · BTC +0.20% · ZEC X-Perp -0.10%", payload["text"])
            self.assertIn("5m · BTC +0.50% · ZEC X-Perp +1.00%", payload["text"])
