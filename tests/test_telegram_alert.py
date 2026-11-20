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
