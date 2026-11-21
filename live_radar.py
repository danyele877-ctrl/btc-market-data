"""Publish a compact, read-only radar result from the running OKX collector."""

import asyncio
from datetime import datetime, timezone

from build_radar_snapshot import build_snapshot, candle_tail
from capture_render_feed import HYBRID_MODE, collect_rest_snapshot, now, websocket_window_ready
from render_bridge_publication import _timestamp, build_compact_alert
from telegram_alert import TelegramAlert
from zec_limit_tracking import LimitTracker, build_limit_plans, shadow_live_plan


def unavailable(reason):
    return {
        "schema_version": 1,
        "prices": {},
        "feed": {"source": HYBRID_MODE, "status": "unavailable", "fresh_at_publication": False},
        "quality": {"engine_ready": False, "required_prices_available": False, "blockers": [reason]},
        "decision": {"side": "NO_SIGNAL", "reason": reason},
    }


def relative_move(btc, zec):
    if len(btc) != 4 or len(zec) != 4 or any(a["completed_at"] != b["completed_at"] for a, b in zip(btc, zec)):
        return None
    return round(100 * (float(zec[-1]["close"]) / float(zec[0]["close"])
                       / (float(btc[-1]["close"]) / float(btc[0]["close"])) - 1), 4)


def btc_regime(bars):
    if len(bars) != 4:
        return "N/D"
    change = float(bars[-1]["close"]) - float(bars[0]["close"])
    mean_range = sum(float(bar["high"]) - float(bar["low"]) for bar in bars) / 4
    return "Rialzista" if change > mean_range else "Ribassista" if change < -mean_range else "Laterale"


class LiveRadar:
    def __init__(self, feed):
        self.feed = feed
        self.snapshot = None
        self.completed_slot = None
        self.completed_snapshot = None
        self.completed_limit_candles = ()
        self.completed_limit_plans = []
        self.telegram = TelegramAlert()
        self.limit_tracker = LimitTracker(self.telegram.connection) if self.telegram.postgres_dsn else None

    def current(self):
        if self.snapshot is None:
            return unavailable("live_snapshot_pending")
        if not websocket_window_ready(self.feed.microstructure_health()):
            return unavailable("live_websocket_not_ready")
        alert = build_compact_alert(self.snapshot, datetime.now(timezone.utc))
        if self.snapshot is self.completed_snapshot and self.completed_limit_plans and alert["decision"]["side"] in ("LONG", "SHORT"):
            plans = self.completed_limit_plans
            alert.update(limit_plan=plans[0], limit_plans=plans)
        return alert

    async def refresh(self):
        health = self.feed.microstructure_health()
        if not self.feed.health()["websocket_connected"] or not websocket_window_ready(health):
            self.snapshot = None
            return
        reference = datetime.now(timezone.utc)
        slot = int(reference.timestamp()) // 300
        if slot == self.completed_slot or reference.timestamp() % 300 > 150:
            return
        # Copy the candle lists on the event loop; the builder validates them
        # and REST supplies independent ticker and derivatives context.
        cache = {
            key: {bar: list(rows) for bar, rows in bars.items()}
            for key, bars in self.feed.state["instruments"].items()
        }
        output, history = await asyncio.to_thread(collect_rest_snapshot, live_instruments=cache)
        health = self.feed.microstructure_health()
        if not self.feed.health()["websocket_connected"] or not websocket_window_ready(health):
            self.snapshot = None
            return
        captured = now()
        output.update(source_mode=HYBRID_MODE, captured_at=captured)
        output["candles"]["payload"]["source_mode"] = HYBRID_MODE
        for key in ("healthz", "candles"):
            output[key]["retrieved_at"] = captured
        output["healthz"]["payload"].update(
            source_mode=HYBRID_MODE,
            websocket_connected=True,
            last_message_at=health.get("last_message_at"),
            last_message_age_seconds=health.get("message_age_seconds"),
            microstructure=health,
        )
        snapshot = await asyncio.to_thread(build_snapshot, output, {}, history)
        self.snapshot = snapshot
        published_at = datetime.now(timezone.utc)
        close = _timestamp(snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["5m"]["latest"]["completed_at"])
        # Retry transient quality failures within the same publication window.
        # A healthy NO_SIGNAL completes the slot just as a confirmed trigger does.
        if (
            close is not None
            and int(close.timestamp()) // 300 == slot
            and (alert := build_compact_alert(snapshot, published_at))["quality"]["engine_ready"]
        ):
            self.completed_slot = slot
            self.completed_snapshot = snapshot
            self.completed_limit_candles = candle_tail(output, "ZEC-USD_UM_XPERP", "5m", count=9)
            self.completed_limit_plans = []
            if alert.get("decision", {}).get("side") in ("LONG", "SHORT"):
                plans = build_limit_plans(alert, slot, self.completed_limit_candles)
                btc = candle_tail(output, "BTC-USDC", "5m", count=4)
                zec = candle_tail(output, "ZEC-USDC", "5m", count=4)
                sign = 1 if plans[0]["side"] == "LONG" else -1
                imbalance = (health.get("book") or {}).get("imbalance_top10")
                cvd = (health.get("cvd") or {}).get("1m")
                for plan in plans:
                    plan.update(regime=btc_regime(btc), relative_zec_btc=relative_move(btc, zec))
                    if plan["variant"] == "SHADOW_RETEST" and not plan["reason"]:
                        if not all(isinstance(value, (int, float)) and value * sign > 0
                                   for value in (imbalance, cvd)):
                            plan["reason"] = "retest senza conferma di book e CVD."
                self.completed_limit_plans = plans

    async def run(self):
        while not self.feed.stop_event.is_set():
            try:
                await self.refresh()
                if (
                    self.snapshot is not None and self.snapshot is self.completed_snapshot
                    and self.telegram.token and self.telegram.chat_id
                ):
                    await asyncio.to_thread(
                        self.telegram.publish, self.current(), self.completed_slot, self.completed_limit_candles
                    )
                if self.limit_tracker:
                    now = datetime.now(timezone.utc)
                    zec = self.feed.state["instruments"].get("ZEC-USD_UM_XPERP", {})
                    shadow = None
                    if self.feed.microstructure_health().get("connected") and self.feed.health()["websocket_connected"]:
                        shadow = shadow_live_plan(now, zec.get("5m", []), self.feed.micro_trade_events)
                    await asyncio.to_thread(
                        self.limit_tracker.update, self.current() if self.snapshot is self.completed_snapshot else {},
                        self.completed_slot,
                        self.completed_limit_candles,
                        list(zec.get("1m", [])),
                        now, shadow,
                    )
            except (Exception, SystemExit) as exc:
                self.snapshot = None
                print(f"Live radar refresh blocked: {exc}", flush=True)
            await asyncio.sleep(15)
