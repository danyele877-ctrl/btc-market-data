"""Send fresh radar triggers to a configured private Telegram chat."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp


class TelegramAlert:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.last_slot = None
        self.postgres_dsn = os.getenv("RADAR_TELEGRAM_DATABASE_URL")
        self.database = os.getenv("RADAR_TELEGRAM_STATE_PATH", "data/telegram-delivery.sqlite3")

    @contextmanager
    def connection(self):
        if self.postgres_dsn:
            import psycopg

            def configure(conn):
                conn.execute("SET statement_timeout = 2000")
                conn.execute("SET lock_timeout = 1000")

            with psycopg.connect(self.postgres_dsn, connect_timeout=3, autocommit=True) as db:
                configure(db)
                yield db, "%s"
        else:
            db = sqlite3.connect(self.database, timeout=1, isolation_level=None)
            try:
                yield db, "?"
            finally:
                db.close()

    def reserve(self, db, param, slot, now):
        db.execute("""CREATE TABLE IF NOT EXISTS delivery (
            slot BIGINT PRIMARY KEY, status TEXT NOT NULL, retry_at DOUBLE PRECISION, attempts INTEGER NOT NULL
        )""")
        return db.execute(f"""
                INSERT INTO delivery VALUES ({param}, 'pending', NULL, 1)
                ON CONFLICT(slot) DO UPDATE SET status='pending', retry_at=NULL, attempts=2
                WHERE delivery.status='rate_limited' AND delivery.retry_at <= {param} AND delivery.attempts=1
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

        rome = ZoneInfo("Europe/Rome")
        captured = _timestamp(compact["captured_at"]).astimezone(rome)
        expires = _timestamp(compact["feed"]["expires_at"]).astimezone(rome)
        last = compact["prices"][ZEC_ID]["last"]
        invalidation = compact["decision"]["invalidation_price"]
        icon = "🟢" if side == "LONG" else "🔴"
        text = (
            f"{icon} <b>{side} confermato · ZEC X-Perp</b>\n"
            f"💵 Prezzo rilevato <b>{float(last):.2f} USD</b>\n"
            f"🕒 Ora del segnale <b>{captured:%H:%M:%S}</b> · {captured:%d/%m/%Y %Z}\n\n"
            f"⏳ Valido fino alle <b>{expires:%H:%M:%S %Z}</b>\n"
            f"🚫 Invalidazione <b>{float(invalidation):.2f} USD</b>\n"
            f"<code>{ZEC_ID}</code> · OKX Europe"
        )
        body = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}).encode()
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.connection() as (db, param):
                if not self.reserve(db, param, slot, now):
                    return
                if validated_side(compact, datetime.now(timezone.utc)) != side:
                    return
                started = time.monotonic()
                status, retry_at = self.send(request, compact)
                db.execute(f"UPDATE delivery SET status={param}, retry_at={param} WHERE slot={param}",
                           (status, retry_at, slot))
                if status != "rate_limited":
                    self.last_slot = slot
                generated = _timestamp(compact["generated_at"])
                age = (datetime.now(timezone.utc) - generated).total_seconds()
                print(f"Telegram radar {status} slot={slot} elapsed_s={time.monotonic()-started:.3f} "
                      f"generated_age_s={age:.3f}", flush=True)
        except Exception as exc:
            if not isinstance(exc, sqlite3.Error) and not self.postgres_dsn:
                raise
            # Database errors may contain credentials or connection details.
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
