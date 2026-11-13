from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

OKX_REST = "https://eea.okx.com"
OKX_WS = "wss://wseea.okx.com/ws/v5/business"
BAR_SECONDS = {"candle1m": 60, "candle5m": 300, "1H": 3600, "4H": 14400}
WS_CHANNELS = ("candle1m", "candle5m")
HISTORY_PAGE_LIMIT = 100
HISTORY_MAX_PAGES = 5
EXPECTED_XPERP_INST_ID = "ZEC-USD_UM_XPERP-310530"
MAX_CANDLES = int(os.getenv("MAX_CANDLES", "500"))
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 60
WS_STALE_SECONDS = 120
REQUEST_TIMEOUT = 15
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "danyele877-ctrl/btc-market-data")
DATA_FILE = Path(os.getenv("CANDLE_DATA_FILE", "data/candles.json"))

# Public market data only. The X-Perp ID and type were checked against OKX's
# public instruments metadata; ZEC-USDT-SWAP is deliberately not included.
DESIRED_INSTRUMENTS = (
    ("SPOT", "BTC-USDC"),
    ("SPOT", "ZEC-USDC"),
    ("FUTURES", "ZEC-USD_UM_XPERP"),
)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def fetch_instruments(inst_type: str, inst_id: str | None = None) -> list[dict[str, Any]]:
    params = {"instType": inst_type}
    if inst_id:
        params["instId"] = inst_id
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{OKX_REST}/api/v5/public/instruments?{query}",
        headers={"User-Agent": "btc-market-data-candle-feed/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    # OKX returns 51001 when an instrument is not listed for this regional
    # API host. Treat it as unavailable so one regional listing cannot stop
    # discovery and subscriptions for the other configured public markets.
    if payload.get("code") == "51001":
        return []
    if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"OKX instrument metadata error: {payload.get('code')} {payload.get('msg')}")
    return payload["data"]

def resolve_instruments(
    spot_rows: list[dict[str, Any]],
    futures_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    rows = {"SPOT": spot_rows, "FUTURES": futures_rows}
    active: list[dict[str, str]] = []
    unavailable: list[str] = []
    for inst_type, inst_id in DESIRED_INSTRUMENTS:
        # For the X-Perp, match only the exact dated exchange instrument.
        # A family match alone could silently select a different expiry.
        expected_id = EXPECTED_XPERP_INST_ID if inst_type == "FUTURES" else inst_id
        match = next(
            (row for row in rows[inst_type]
             if row.get("state") == "live" and row.get("instId") == expected_id),
            None,
        )
        if match:
            resolved_id = str(match.get("instId", inst_id))
            item = {"instType": inst_type, "instId": resolved_id}
            if inst_type == "FUTURES":
                if resolved_id != EXPECTED_XPERP_INST_ID:
                    unavailable.append(inst_id)
                    continue
                item["stream_key"] = "ZEC-USD_UM_XPERP"
            active.append(item)
        else:
            unavailable.append(inst_id)
    return active, unavailable

def parse_candle(row: list[Any], bar: str) -> dict[str, Any] | None:
    """Return a normalized, completed candle; ignore malformed/open rows."""
    if bar not in BAR_SECONDS or not isinstance(row, (list, tuple)) or len(row) < 9:
        return None
    if str(row[8]) != "1":
        return None
    try:
        ts_ms = int(row[0])
        values = [str(row[index]) for index in range(1, 8)]
        if any(not value for value in values):
            return None
        open_time = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
        close_time = datetime.fromtimestamp(ts_ms / 1000 + BAR_SECONDS[bar], timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return {
        "ts_ms": ts_ms,
        "timestamp": iso_utc(open_time),
        "completed_at": iso_utc(close_time),
        "open": values[0],
        "high": values[1],
        "low": values[2],
        "close": values[3],
        "volume": values[4],
        "volume_currency": values[5],
        "volume_quote": values[6],
    }

def merge_candles(target: list[dict[str, Any]], candles: list[dict[str, Any]]) -> bool:
    by_time = {int(item["ts_ms"]): item for item in target}
    for candle in candles:
        by_time[int(candle["ts_ms"])] = candle
    merged = [by_time[key] for key in sorted(by_time)][-MAX_CANDLES:]
    changed = merged != target
    target[:] = merged
    return changed

def new_state() -> dict[str, Any]:
    streams: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for _, inst_id in DESIRED_INSTRUMENTS:
        streams[inst_id] = {"1m": [], "5m": [], "1H": [], "4H": []}
    return {
        "schema_version": 1,
        "generated_at": iso_utc(),
        "service": {
            "status": "starting",
            "connected": False,
            "last_message_at": None,
            "last_error": None,
            "reconnects": 0,
            "active_instruments": [],
            "unavailable_instruments": [],
        },
        "instruments": streams,
    }

class CandleFeed:
    def __init__(self, data_file: Path = DATA_FILE):
        self.data_file = data_file
        self.state = new_state()
        self.started = time.monotonic()
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.last_bridge_dispatch_minute: int | None = None

    def health(self) -> dict[str, Any]:
        now = utc_now()
        service = dict(self.state["service"])
        message_time = parse_iso(service.get("last_message_at"))
        message_age = (now - message_time).total_seconds() if message_time else None
        streams: dict[str, dict[str, Any]] = {}
        for inst_id, bars in self.state["instruments"].items():
            for bar, candles in bars.items():
                newest = candles[-1] if candles else None
                completed = parse_iso(newest.get("completed_at")) if newest else None
                age = (now - completed).total_seconds() if completed else None
                interval = {"1m": 60, "5m": 300, "1H": 3600, "4H": 14400}[bar]
                stale_after = interval * 3
                streams[f"{inst_id}:{bar}"] = {
                    "candle_count": len(candles),
                    "history_ready_200": len(candles) >= 200,
                    "last_candle_at": newest.get("timestamp") if newest else None,
                    "last_completed_at": newest.get("completed_at") if newest else None,
                    "age_seconds": round(age, 1) if age is not None else None,
                    "stale_after_seconds": stale_after,
                    "stale": age is None or age < -60 or age > stale_after,
                }
        connected = bool(service.get("connected"))
        active_ids = {
            item.get("stream_key", item["instId"])
            for item in service.get("active_instruments", [])
        }
        active_streams = [
            streams[f"{inst_id}:{bar}"]
            for inst_id in active_ids for bar in ("1m", "5m")
        ]
        history_streams = [
            streams[f"{inst_id}:{bar}"]
            for inst_id in active_ids if inst_id == "ZEC-USD_UM_XPERP"
            for bar in ("1H", "4H")
        ]
        streams_fresh = bool(active_streams) and all(not item["stale"] for item in active_streams)
        history_ready = bool(history_streams) and all(
            item["history_ready_200"] and not item["stale"] for item in history_streams
        )
        coverage_complete = not service.get("unavailable_instruments")
        status = (
            "ok"
            if connected and message_age is not None and message_age <= WS_STALE_SECONDS
            and streams_fresh and history_ready and coverage_complete
            else "degraded"
        )
        return {
            "status": status,
            "generated_at": iso_utc(now),
            "uptime_seconds": round(time.monotonic() - self.started),
            "websocket_connected": connected,
            "last_message_at": service.get("last_message_at"),
            "last_message_age_seconds": round(message_age, 1) if message_age is not None else None,
            "reconnects": service.get("reconnects", 0),
            "active_instruments": service.get("active_instruments", []),
            "unavailable_instruments": service.get("unavailable_instruments", []),
            "streams": streams,
            "history_ready_200": history_ready,
            "last_error": service.get("last_error"),
        }

    async def persist(self) -> None:
        self.state["generated_at"] = iso_utc()
        payload = json.dumps(self.state, ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(self._atomic_write, payload)

    def _atomic_write(self, payload: str) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.data_file)

    async def handle_message(self, message: str) -> None:
        if message == "ping":
            return
        payload = json.loads(message)
        if payload.get("event") == "error":
            raise RuntimeError(f"OKX websocket error: {payload.get('code')} {payload.get('msg')}")
        if payload.get("event") == "notice":
            raise RuntimeError(f"OKX websocket notice: {payload.get('code')} {payload.get('msg')}")
        if payload.get("event"):
            return
        arg = payload.get("arg") or {}
        exchange_inst_id = arg.get("instId")
        channel = arg.get("channel")
        stream_ids = {
            item["instId"]: item.get("stream_key", item["instId"])
            for item in self.state["service"].get("active_instruments", [])
        }
        inst_id = stream_ids.get(exchange_inst_id, exchange_inst_id)
        if inst_id not in self.state["instruments"] or channel not in WS_CHANNELS:
            return
        rows = payload.get("data")
        if not isinstance(rows, list):
            return
        self.state["service"]["last_message_at"] = iso_utc()
        parsed = [item for item in (parse_candle(row, channel) for row in rows) if item]
        if not parsed:
            return
        bar = channel.removeprefix("candle")
        async with self.lock:
            merge_candles(self.state["instruments"][inst_id][bar], parsed)
            self.state["service"]["status"] = "ok"
            await self.persist()

        # Dispatch once per completed UTC minute; coalesce all instruments.
        if channel == "candle1m" and GITHUB_TOKEN:
            latest = max(parsed, key=lambda item: int(item["ts_ms"]))
            minute_key = int(latest["ts_ms"]) // 60000
            if self.last_bridge_dispatch_minute != minute_key:
                self.last_bridge_dispatch_minute = minute_key
                asyncio.create_task(dispatch_bridge_refresh(latest["completed_at"]))

    async def bootstrap_history(self, active: list[dict[str, str]]) -> None:
        """Load 300 completed 1H/4H candles for the exact EEA ZEC X-Perp."""
        if not any(x.get("instId") == EXPECTED_XPERP_INST_ID for x in active):
            raise RuntimeError(
                f"Expected exact EEA instrument {EXPECTED_XPERP_INST_ID} not found; "
                "refusing to substitute another contract"
            )

        async def load(bar_name: str, interval: str) -> None:
            # OKX history-candles limits each page to 100 rows. Walk backwards
            # with the documented "after" cursor until 200 completed candles
            # are collected; never assume a single 300-row response is valid.
            collected: dict[int, dict[str, Any]] = {}
            cursor: int | None = None
            for _page in range(HISTORY_MAX_PAGES):
                params = {
                    "instId": EXPECTED_XPERP_INST_ID,
                    "bar": interval,
                    "limit": str(HISTORY_PAGE_LIMIT),
                }
                if cursor is not None:
                    params["after"] = str(cursor)
                query = urllib.parse.urlencode(params)
                request = urllib.request.Request(
                    f"{OKX_REST}/api/v5/market/history-candles?{query}",
                    headers={"User-Agent": "btc-market-data-candle-feed/1.0", "Accept": "application/json"},
                )
                def fetch(req=request):
                    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                        status = response.status
                        payload = json.loads(response.read().decode("utf-8"))
                    if status != 200 or payload.get("code") != "0":
                        raise RuntimeError(
                            f"OKX history {interval} HTTP {status} API {payload.get('code')} {payload.get('msg')}"
                        )
                    return payload
                payload = await asyncio.to_thread(fetch)
                rows = payload.get("data", [])
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    candle = parse_candle(row, interval)
                    if candle is not None:
                        collected[int(candle["ts_ms"])] = candle
                try:
                    oldest = min(int(row[0]) for row in rows if isinstance(row, (list, tuple)) and row)
                except (TypeError, ValueError):
                    break
                if cursor is not None and oldest >= cursor:
                    break
                cursor = oldest
                if len(collected) >= 200:
                    break
            parsed = [collected[key] for key in sorted(collected)]
            if len(parsed) < 200:
                raise RuntimeError(
                    f"Only {len(parsed)} completed valid {interval} candles for {EXPECTED_XPERP_INST_ID}; need 200"
                )
            async with self.lock:
                merge_candles(self.state["instruments"]["ZEC-USD_UM_XPERP"][bar_name], parsed)
                await self.persist()

        await asyncio.gather(load("1H", "1H"), load("4H", "4H"))

    async def discover(self) -> list[dict[str, str]]:
        spot_instruments = [
            (inst_type, inst_id)
            for inst_type, inst_id in DESIRED_INSTRUMENTS
            if inst_type == "SPOT"
        ]
        metadata_requests = [
            asyncio.to_thread(fetch_instruments, inst_type, inst_id)
            for inst_type, inst_id in spot_instruments
        ]
        # The EEA catalog can expose a futures family under its dated
        # exchange ID. Fetch the public futures catalog and resolve by family.
        metadata_requests.append(asyncio.to_thread(fetch_instruments, "FUTURES"))
        metadata_rows = await asyncio.gather(*metadata_requests)
        rows_by_type: dict[str, list[dict[str, Any]]] = {"SPOT": [], "FUTURES": []}
        for (inst_type, _inst_id), rows in zip(spot_instruments, metadata_rows):
            rows_by_type[inst_type].extend(rows)
        rows_by_type["FUTURES"].extend(metadata_rows[-1])
        active, unavailable = resolve_instruments(rows_by_type["SPOT"], rows_by_type["FUTURES"])
        self.state["service"]["active_instruments"] = active
        self.state["service"]["unavailable_instruments"] = unavailable
        return active

    async def run_websocket(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while not self.stop_event.is_set():
            try:
                active = await self.discover()
                if not active:
                    raise RuntimeError("No configured OKX instruments are live")
                # Historical warm-up must never prevent the live 1m/5m feed
                # from connecting. Keep the diagnostic visible and retry history
                # periodically while the WebSocket remains active.
                try:
                    await self.bootstrap_history(active)
                    last_history_refresh = time.monotonic()
                except Exception as history_exc:
                    last_history_refresh = time.monotonic()
                    self.state["service"]["last_error"] = (
                        f"History bootstrap: {type(history_exc).__name__}: {str(history_exc)}"
                    )[:500]
                    await self.persist()
                subscriptions = [
                    {"channel": channel, "instId": item["instId"]}
                    for item in active for channel in WS_CHANNELS
                ]
                async with websockets.connect(
                    OKX_WS, open_timeout=20, close_timeout=10, ping_interval=None,
                    max_size=2**20,
                ) as websocket:
                    await websocket.send(json.dumps({"op": "subscribe", "args": subscriptions}))
                    self.state["service"]["connected"] = True
                    self.state["service"]["status"] = "connecting"
                    await self.persist()
                    delay = RECONNECT_MIN_SECONDS
                    while True:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=25)
                        except asyncio.TimeoutError:
                            await websocket.send("ping")
                            message = await asyncio.wait_for(websocket.recv(), timeout=10)
                        if message == "ping":
                            await websocket.send("pong")
                            self.state["service"]["last_message_at"] = iso_utc()
                        elif message == "pong":
                            self.state["service"]["last_message_at"] = iso_utc()
                        else:
                            await self.handle_message(message)
                        # Refresh the completed 1H/4H history every 15 minutes.
                        # Without this, the 1H/4H health checks inevitably go stale
                        # even while the live WebSocket is healthy.
                        if (
                            any(x.get("instId") == EXPECTED_XPERP_INST_ID for x in active)
                            and time.monotonic() - last_history_refresh >= 900
                        ):
                            try:
                                await self.bootstrap_history(active)
                                self.state["service"]["last_error"] = None
                            except Exception as history_exc:
                                self.state["service"]["last_error"] = (
                                    f"History refresh: {type(history_exc).__name__}: {str(history_exc)}"
                                )[:500]
                            last_history_refresh = time.monotonic()
                            await self.persist()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["service"]["connected"] = False
                self.state["service"]["status"] = "degraded"
                self.state["service"]["last_error"] = f"{type(exc).__name__}: {str(exc)}"[:500]
                self.state["service"]["reconnects"] += 1
                await self.persist()
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
            finally:
                self.state["service"]["connected"] = False

async def dispatch_bridge_refresh(completed_at: str) -> None:
    """Ask GitHub Actions to publish a fresh multi-source bridge snapshot."""
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/dispatches"
    body = json.dumps({"event_type": "refresh_render_feed", "client_payload": {"completed_at": completed_at}}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "btc-market-data-render-bridge/1.0",
        "Content-Type": "application/json",
    })
    try:
        def send():
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.status
        status = await asyncio.to_thread(send)
        print(f"GitHub bridge dispatch accepted: HTTP {status}; candle={completed_at}", flush=True)
    except Exception as exc:
        print(f"GitHub bridge dispatch failed: {type(exc).__name__}: {exc}", flush=True)

async def write_response(writer: asyncio.StreamWriter, status: str, body: bytes, content_type: str) -> None:
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()

async def handle_http(feed: CandleFeed, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = request_line.decode("ascii", "replace").strip().split()
        if len(parts) < 2 or parts[0] != "GET":
            await write_response(writer, "405 Method Not Allowed", b'{"error":"GET only"}', "application/json")
            return
        path = urllib.parse.urlsplit(parts[1]).path
        while await reader.readline() not in (b"\r\n", b"\n", b""):
            pass
        if path in ("/", "/candles.json"):
            body = json.dumps(feed.state, ensure_ascii=False).encode("utf-8")
            await write_response(writer, "200 OK", body, "application/json; charset=utf-8")
        elif path == "/healthz":
            body = json.dumps(feed.health(), ensure_ascii=False).encode("utf-8")
            await write_response(writer, "200 OK", body, "application/json; charset=utf-8")
        else:
            await write_response(writer, "404 Not Found", b'{"error":"not found"}', "application/json")
    except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass

async def main() -> None:
    feed = CandleFeed()
    port = int(os.getenv("PORT", "8080"))
    server = await asyncio.start_server(
        lambda reader, writer: handle_http(feed, reader, writer),
        host="0.0.0.0", port=port, limit=8192,
    )
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, feed.stop_event.set)
        except NotImplementedError:
            pass
    ws_task = asyncio.create_task(feed.run_websocket())
    print(f"Read-only OKX candle feed listening on 0.0.0.0:{port}", flush=True)
    try:
        async with server:
            await feed.stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        feed.stop_event.set()
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)

def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
