"""Send fresh radar triggers to a configured private Telegram chat."""

import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID


class TelegramAlert:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.last_slot = None

    def publish(self, compact, slot):
        if not self.token or not self.chat_id.isdecimal() or self.last_slot == slot:
            return
        try:
            side = validated_side(compact, datetime.now(timezone.utc))
        except (KeyError, TypeError, ValueError):
            return
        if side not in ("LONG", "SHORT"):
            return

        # Allow one send attempt per slot. A timeout can occur after delivery.
        self.last_slot = slot
        text = (
            f"Radar BTC + ZEC | {side}\n{ZEC_ID}\n"
            f"Acquisizione UTC: {compact['captured_at']}\n"
            f"Scadenza UTC: {compact['feed']['expires_at']}\n"
            f"Invalidazione: {compact['decision']['invalidation_price']}"
        )
        body = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=8) as response:
                accepted = json.load(response).get("ok") is True
        except (OSError, ValueError):
            accepted = False
        # Exception strings can contain the URL and bot token.
        print("Telegram radar alert accepted" if accepted else "Telegram radar alert unverified", flush=True)
