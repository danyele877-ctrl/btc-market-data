"""Publish a compact, read-only radar result from the running OKX collector."""

import asyncio
from datetime import datetime, timezone

from build_radar_snapshot import build_snapshot
from capture_render_feed import HYBRID_MODE, collect_rest_snapshot, now, websocket_window_ready
from render_bridge_publication import _timestamp, build_compact_alert
from telegram_alert import TelegramAlert


def unavailable(reason):
    return {
        "schema_version": 1,
        "prices": {},
        "feed": {"source": HYBRID_MODE, "status": "unavailable", "fresh_at_publication": False},
        "quality": {"engine_ready": False, "required_prices_available": False, "blockers": [reason]},
        "decision": {"side": "NO_SIGNAL", "reason": reason},
    }


class LiveRadar:
    def __init__(self, feed):
        self.feed = feed
        self.snapshot = None
        self.completed_slot = None
        self.telegram = TelegramAlert()

    def current(self):
        if self.snapshot is None:
            return unavailable("live_snapshot_pending")
        if not websocket_window_ready(self.feed.microstructure_health()):
            return unavailable("live_websocket_not_ready")
        return build_compact_alert(self.snapshot, datetime.now(timezone.utc))

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
            and build_compact_alert(snapshot, published_at)["quality"]["engine_ready"]
        ):
            self.completed_slot = slot
            if self.telegram.token and self.telegram.chat_id:
                await asyncio.to_thread(self.telegram.publish, self.current(), slot)

    async def run(self):
        while not self.feed.stop_event.is_set():
            try:
                await self.refresh()
            except (Exception, SystemExit) as exc:
                self.snapshot = None
                print(f"Live radar refresh blocked: {exc}", flush=True)
            await asyncio.sleep(15)
