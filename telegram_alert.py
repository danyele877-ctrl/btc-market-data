"""Send fresh radar triggers to a configured private Telegram chat."""

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp


class TelegramAlert:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.last_slot = None
        self.database = os.getenv("RADAR_TELEGRAM_STATE_PATH", "data/telegram-delivery.sqlite3")

    def reserve(self, db, slot, now):
        db.execute("""CREATE TABLE IF NOT EXISTS delivery (
            slot INTEGER PRIMARY KEY, status TEXT NOT NULL, retry_at REAL, attempts INTEGER NOT NULL
        )""")
        with db:
            return db.execute("""
                INSERT INTO delivery VALUES (?, 'pending', NULL, 1)
                ON CONFLICT(slot) DO UPDATE SET status='pending', retry_at=NULL, attempts=2
                WHERE status='rate_limited' AND retry_at <= ? AND attempts=1
            """, (slot, now.timestamp())).rowcount == 1

    def publish(self, compact, slot):
        if not self.token or not self.chat_id.isdecimal() or self.last_slot == slot:
            return
        try:
            now = datetime.now(timezone.utc)
            side = validated_side(compact, now)
        except (KeyError, TypeError, ValueError):
            return
        if side not in ("LONG", "SHORT"):
            return

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
            with closing(sqlite3.connect(self.database, timeout=1)) as db:
                if not self.reserve(db, slot, now):
                    return
                if validated_side(compact, datetime.now(timezone.utc)) != side:
                    return
                started = time.monotonic()
                status, retry_at = self.send(request, compact)
                with db:
                    db.execute("UPDATE delivery SET status=?, retry_at=? WHERE slot=?",
                               (status, retry_at, slot))
                if status != "rate_limited":
                    self.last_slot = slot
                generated = _timestamp(compact["generated_at"])
                age = (datetime.now(timezone.utc) - generated).total_seconds()
                print(f"Telegram radar {status} slot={slot} elapsed_s={time.monotonic()-started:.3f} "
                      f"generated_age_s={age:.3f}", flush=True)
        except sqlite3.Error:
            print("Telegram radar delivery ledger unavailable", flush=True)

    def send(self, request, compact):
        try:
            with urlopen(request, timeout=8) as response:
                if json.load(response).get("ok") is True:
                    return "accepted", None
        except HTTPError as exc:
            with exc:
                if exc.code == 429:
                    try:
                        wait = json.load(exc)["parameters"]["retry_after"]
                        if type(wait) is int and wait >= 0:
                            retry_at = time.time() + wait
                            if retry_at < _timestamp(compact["feed"]["expires_at"]).timestamp():
                                return "rate_limited", retry_at
                    except (KeyError, TypeError, ValueError, OSError):
                        pass
        except (OSError, ValueError):
            pass
        # A timeout may follow delivery. Keep the reservation to avoid a repeat.
        return "unverified", None
