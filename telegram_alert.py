"""Send fresh radar triggers to a configured private Telegram chat."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from math import isfinite
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp
from zec_limit_costs import limit_economics


def limit_levels(side, last, invalidation, candles, signal_time, entry_fraction=0.25, retest=False):
    try:
        closes = [_timestamp(row["completed_at"]) for row in candles]
        if (len(closes) != 9 or closes[-1] > signal_time
                or signal_time - closes[-1] >= timedelta(minutes=5)
                or any(b - a != timedelta(minutes=5) for a, b in zip(closes, closes[1:]))):
            return None, "serie 5m incompleta."
        prices = [tuple(float(row[key]) for key in ("low", "close", "high")) for row in candles]
        if any(not (0 < low <= close <= high and isfinite(high)) for low, close, high in prices):
            return None, "prezzi 5m non validi."
        mean_range = sum(high - low for low, _, high in prices) / 9
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "serie 5m non disponibile."

    direction = 1 if side == "LONG" else -1
    entry = round(prices[-1][1] - direction * mean_range * entry_fraction, 2)
    if retest:
        level = (max(row[2] for row in prices[:-2]) if direction > 0
                 else min(row[0] for row in prices[:-2]))
        low, close, high = prices[-1]
        if (direction * (prices[-2][1] - level) <= 0 or not low <= level <= high
                or direction * (close - level) <= 0):
            return None, "retest 5m non confermato."
        entry = round(level, 2)
    stop = round(entry - direction * 1.5 * mean_range, 2)
    tp1 = round(entry + direction * 1.5 * mean_range, 2)
    tp2 = round(entry + direction * 2.5 * mean_range, 2)
    if (min(entry, stop, tp1, tp2) <= 0
            or not all(isfinite(level) for level in (entry, stop, tp1, tp2))
            or not direction * stop < direction * entry < direction * tp1 < direction * tp2
            or not direction * invalidation < direction * entry < direction * last):
        return None, "ingresso incompatibile con prezzo o invalidazione."

    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2}, None


def limit_suggestion(side, last, invalidation, candles, signal_time, expires):
    levels, reason = limit_levels(side, last, invalidation, candles, signal_time)
    if levels is None:
        return f"📋 <b>LIMIT NON ATTIVABILE</b> · {reason}"
    return format_limit({**levels, "side": side}, min(signal_time + timedelta(minutes=5), expires))


def format_limit(plan, deadline):
    if plan.get("reason"):
        return f"📋 <b>LIMIT NON ATTIVABILE</b> · {plan['reason']}"
    side, entry, stop, tp1, tp2 = (plan[key] for key in ("side", "entry", "stop", "tp1", "tp2"))
    costs = limit_economics(plan)
    return (
        f"📋 <b>LIMIT {side} · {entry:.2f} USD</b>\n"
        f"SL <b>{stop:.2f}</b> · TP1 <b>{tp1:.2f}</b> · TP2 <b>{tp2:.2f} USD</b>\n"
        f"Netto sul nozionale · TP1 <b>{costs['tp1_net_pct']:+.2f}%</b> · SL "
        f"<b>{-costs['stop_loss_net_pct']:+.2f}%</b> · R/R <b>{costs['net_reward_risk']:.2f}:1</b>\n"
        f"Stima · Fee {costs['fee_bps_per_side'] / 100:.2f}% per lato · "
        f"Slippage SL {costs['exit_slippage_bps'] / 100:.2f}% · Funding escluso\n"
        f"Valido fino alle <b>{deadline:%H:%M:%S %Z}</b> o fino all’invalidazione."
    )


class TelegramAlert:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.last_slot = None
        self.last_macro_slot = None
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

    def publish(self, compact, slot, candles=()):
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
        shared = compact.get("limit_plan")
        if shared:
            deadline = datetime.fromtimestamp(shared["fill_by"], rome)
            limit = format_limit(shared, deadline) + f"\nPiano <code>{shared['id']}</code>"
        else:
            limit = limit_suggestion(side, float(last), float(invalidation), candles, captured, expires)
        text = (
            f"{icon} <b>{side} confermato · ZEC X-Perp</b>\n"
            f"💵 Prezzo rilevato <b>{float(last):.2f} USD</b>\n"
            f"🕒 Ora del segnale <b>{captured:%H:%M:%S}</b> · {captured:%d/%m/%Y %Z}\n\n"
            f"⏳ Valido fino alle <b>{expires:%H:%M:%S %Z}</b>\n"
            f"🚫 Invalidazione <b>{float(invalidation):.2f} USD</b>\n"
            f"\n{limit}\n\n"
            f"<code>{ZEC_ID}</code> · OKX Europe"
        )
        request = self.message_request(text)
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

    def message_request(self, text):
        body = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}).encode()
        return Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )

    def publish_macro(self, context, *, now=None):
        if not self.token or not self.chat_id.isdecimal():
            return
        now = now or datetime.now(timezone.utc)
        event = context.get("event")
        if not event:
            return
        timestamp = now.timestamp()
        event_at = _timestamp(event["utc"])
        status = context["status"]
        title = escape(event["title"])
        local_time = event_at.astimezone(ZoneInfo("Europe/Rome"))
        if status == "macro_pause" and context["pause_from"] <= timestamp < event_at.timestamp():
            phase, expires = 0, event_at.timestamp()
            text = (
                f"📅 <b>EVENTO MACRO · {title}</b>\n"
                f"🕒 <b>{local_time:%H:%M %Z}</b> · {local_time:%d/%m/%Y}\n"
                "Possibile aumento della volatilità su BTC e ZEC. Direzione in attesa."
            )
        elif status in ("watching", "retest_observed") and context.get("side") in ("LONG", "SHORT"):
            watch_at = context["watch_at"]
            phase, expires = 1, min(context["watch_until"], watch_at + 90)
            if not watch_at <= timestamp < expires:
                return
            observed = datetime.fromtimestamp(watch_at, local_time.tzinfo)
            text = (
                f"⚡ <b>EVENTO MACRO · {title}</b>\n"
                f"<b>{context['side']} IN OSSERVAZIONE · SHADOW</b>\n"
                f"🕒 {observed:%H:%M:%S %Z} · BTC e ZEC concordi su candele chiuse 1m.\n"
                f"Rottura ZEC X-Perp <b>{context['breakout_level']:.2f} USD</b>\n"
                "Avviso diagnostico · LIMIT non attivabile."
            )
        else:
            return

        # Negative keys keep macro phases separate from positive BASELINE slots.
        slot = -(int(event_at.timestamp()) * 2 + phase)
        if self.last_macro_slot == slot:
            return
        deadline = datetime.fromtimestamp(expires, timezone.utc)
        try:
            with self.connection() as (db, param):
                if not self.reserve(db, param, slot, now):
                    return
                status, retry_at = self.send(self.message_request(text), {"feed": {"expires_at": deadline.isoformat()}})
                db.execute(f"UPDATE delivery SET status={param}, retry_at={param} WHERE slot={param}",
                           (status, retry_at, slot))
                if status != "rate_limited":
                    self.last_macro_slot = slot
                print(f"Telegram macro {status} slot={slot}", flush=True)
        except Exception:
            # Keep failures isolated from the radar and avoid logging credentials.
            print("Telegram macro delivery ledger unavailable", flush=True)

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
