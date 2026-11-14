from __future__ import annotations

import asyncio
import json
from collections import deque
import os
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

OKX_REST = "https://eea.okx.com"
OKX_WS = "wss://wseea.okx.com/ws/v5/business"
OKX_PUBLIC_WS = "wss://wseea.okx.com/ws/v5/public"
BAR_SECONDS = {"candle1m": 60, "candle5m": 300, "1H": 3600, "4H": 14400}
WS_CHANNELS = ("candle1m", "candle5m")
HISTORY_PAGE_LIMIT = 100
HISTORY_MAX_PAGES = 5
HISTORY_TARGET_CANDLES = 300
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
        "microstructure": {
            "status": "starting",
            "connected": False,
            "connected_since_ms": None,
            "last_message_at": None,
            "last_trade_at": None,
            "last_book_at": None,
            "last_error": None,
            "reconnects": 0,
            "last_trade_id": None,
            "trade_gap_count": 0,
            "trade_message_count": 0,
            "book_valid": False,
            "book_seq_id": None,
            "book_gap_count": 0,
            "book": {"bids": [], "asks": [], "imbalance_top5": None, "imbalance_top10": None, "imbalance_top20": None},
            "cvd": {
                "unit": "contracts",
                "1m": None,
                "5m": None,
                "15m": None,
                "ready_1m": False,
                "ready_5m": False,
                "ready_15m": False
            }
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
        self.micro_trade_events = deque()
        self.micro_book = {"bids": {}, "asks": {}, "seq_id": None, "valid": False}
        self.micro_last_persist = 0.0

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
                gap_count = 0
                for previous, current in zip(candles, candles[1:]):
                    if int(current["ts_ms"]) - int(previous["ts_ms"]) != interval * 1000:
                        gap_count += 1
                streams[f"{inst_id}:{bar}"] = {
                    "candle_count": len(candles),
                    "history_ready_200": len(candles) >= 200 and gap_count == 0,
                    "gap_count": gap_count,
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
        intraday_history_ready = bool(active_streams) and all(
            item["history_ready_200"] and not item["stale"] for item in active_streams
        )
        history_ready = bool(history_streams) and all(
            item["history_ready_200"] and not item["stale"] for item in history_streams
        ) and intraday_history_ready
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
            "microstructure": self.microstructure_health(),
        }

    def microstructure_health(self) -> dict[str, Any]:
        micro = self.state["microstructure"]
        now_ms = int(utc_now().timestamp() * 1000)
        self._refresh_micro_cvd(now_ms)
        def age(value):
            try:
                return round((now_ms - int(value)) / 1000, 1) if value else None
            except (TypeError, ValueError):
                return None
        result = dict(micro)
        result["message_age_seconds"] = age(
            int(parse_iso(micro.get("last_message_at")).timestamp() * 1000)
            if parse_iso(micro.get("last_message_at")) else None
        )
        result["trade_age_seconds"] = age(micro.get("last_trade_at"))
        result["book_age_seconds"] = age(micro.get("last_book_at"))
        result["book_valid"] = bool(micro.get("book_valid") and self.micro_book.get("valid"))
        result["connected"] = bool(micro.get("connected"))
        result["cvd"] = dict(micro.get("cvd") or {})
        return result

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

    def _refresh_micro_cvd(self, now_ms: int) -> None:
        micro = self.state["microstructure"]
        cvd = micro["cvd"]
        connected_since = micro.get("connected_since_ms")
        for label, window_seconds in (("1m", 60), ("5m", 300), ("15m", 900)):
            start_ms = now_ms - window_seconds * 1000
            window = [item for item in self.micro_trade_events if item["ts_ms"] >= start_ms]
            ready = (
                connected_since is not None
                and int(connected_since) <= start_ms
                and not any(item.get("gap_before") for item in window)
            )
            cvd[f"ready_{label}"] = bool(ready)
            cvd[label] = round(sum(item["delta_contracts"] for item in window), 4) if ready else None

    def _apply_book_levels(self, side: str, rows: list) -> None:
        levels = self.micro_book[side]
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                price = str(row[0])
                size = float(row[1])
            except (TypeError, ValueError):
                continue
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size

    def _book_summary(self) -> dict[str, Any]:
        bids = sorted(
            ((float(price), size) for price, size in self.micro_book["bids"].items()),
            key=lambda item: item[0], reverse=True
        )
        asks = sorted(
            ((float(price), size) for price, size in self.micro_book["asks"].items()),
            key=lambda item: item[0]
        )
        def imbalance(depth: int):
            bid_size = sum(size for _, size in bids[:depth])
            ask_size = sum(size for _, size in asks[:depth])
            total = bid_size + ask_size
            return round((bid_size - ask_size) / total, 6) if total else None
        return {
            "bids": [[str(price), str(size)] for price, size in bids[:20]],
            "asks": [[str(price), str(size)] for price, size in asks[:20]],
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "imbalance_top5": imbalance(5),
            "imbalance_top10": imbalance(10),
            "imbalance_top20": imbalance(20),
        }

    async def handle_microstructure_message(self, message: str) -> None:
        if message in ("ping", "pong"):
            return
        payload = json.loads(message)
        if payload.get("event") == "error":
            raise RuntimeError(f"OKX public WS error: {payload.get('code')} {payload.get('msg')}")
        if payload.get("event"):
            return
        arg = payload.get("arg") or {}
        if arg.get("instId") != EXPECTED_XPERP_INST_ID:
            return
        channel = arg.get("channel")
        rows = payload.get("data")
        if not isinstance(rows, list):
            return

        now_ms = int(utc_now().timestamp() * 1000)
        micro = self.state["microstructure"]
        micro["last_message_at"] = iso_utc()

        if channel == "trades":
            for row in rows:
                try:
                    trade_id = int(row.get("tradeId"))
                    size = float(row.get("sz"))
                    count = max(1, int(row.get("count", 1)))
                    ts_ms = int(row.get("ts"))
                except (TypeError, ValueError):
                    continue
                side = row.get("side")
                if size <= 0 or side not in ("buy", "sell"):
                    continue
                previous_id = micro.get("last_trade_id")
                first_id = trade_id - count + 1
                gap_before = False
                if previous_id is not None:
                    if trade_id <= int(previous_id):
                        continue
                    if first_id != int(previous_id) + 1:
                        gap_before = True
                        micro["trade_gap_count"] += 1
                delta = size if side == "buy" else -size
                self.micro_trade_events.append({
                    "ts_ms": ts_ms,
                    "delta_contracts": delta,
                    "gap_before": gap_before,
                })
                micro["last_trade_id"] = trade_id
                micro["last_trade_at"] = ts_ms
                micro["trade_message_count"] += 1
            cutoff = now_ms - 15 * 60 * 1000
            while self.micro_trade_events and self.micro_trade_events[0]["ts_ms"] < cutoff:
                self.micro_trade_events.popleft()
            self._refresh_micro_cvd(now_ms)

        elif channel == "books":
            row = rows[0] if rows else None
            if not isinstance(row, dict):
                return
            action = payload.get("action")
            try:
                seq_id = int(row.get("seqId"))
                prev_seq_id = int(row.get("prevSeqId", -1))
                exchange_ts = int(row.get("ts"))
            except (TypeError, ValueError):
                return

            if action == "snapshot":
                self.micro_book = {"bids": {}, "asks": {}, "seq_id": seq_id, "valid": True}
                self._apply_book_levels("bids", row.get("bids", []))
                self._apply_book_levels("asks", row.get("asks", []))
            elif action == "update":
                current_seq = self.micro_book.get("seq_id")
                if not self.micro_book.get("valid") or current_seq is None or prev_seq_id != int(current_seq):
                    self.micro_book["valid"] = False
                    micro["book_valid"] = False
                    micro["book_gap_count"] += 1
                else:
                    self._apply_book_levels("bids", row.get("bids", []))
                    self._apply_book_levels("asks", row.get("asks", []))
                    self.micro_book["seq_id"] = seq_id
            else:
                return

            micro["book_valid"] = bool(self.micro_book.get("valid"))
            micro["book_seq_id"] = self.micro_book.get("seq_id")
            micro["last_book_at"] = exchange_ts
            if micro["book_valid"]:
                micro["book"] = self._book_summary()

        else:
            return

        # Persist the live microstructure state at most once per second.
        if time.monotonic() - self.micro_last_persist >= 1.0:
            self.micro_last_persist = time.monotonic()
            await self.persist()

    async def run_microstructure_websocket(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        micro = self.state["microstructure"]
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    OKX_PUBLIC_WS, open_timeout=20, close_timeout=10,
                    ping_interval=None, max_size=2**20,
                ) as websocket:
                    args = [
                        {"channel": "trades", "instId": EXPECTED_XPERP_INST_ID},
                        {"channel": "books", "instId": EXPECTED_XPERP_INST_ID},
                    ]
                    await websocket.send(json.dumps({"op": "subscribe", "args": args}))
                    micro["connected"] = True
                    micro["status"] = "warming"
                    micro["connected_since_ms"] = int(utc_now().timestamp() * 1000)
                    micro["last_error"] = None
                    micro["book_valid"] = False
                    self.micro_book = {"bids": {}, "asks": {}, "seq_id": None, "valid": False}
                    self.micro_trade_events.clear()
                    self._refresh_micro_cvd(int(utc_now().timestamp() * 1000))
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
                            micro["last_message_at"] = iso_utc()
                        elif message == "pong":
                            micro["last_message_at"] = iso_utc()
                        else:
                            await self.handle_microstructure_message(message)
                        if micro.get("book_valid") and micro.get("last_trade_at"):
                            micro["status"] = "ok"
                        else:
                            micro["status"] = "warming"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                micro["connected"] = False
                micro["status"] = "degraded"
                micro["book_valid"] = False
                self.micro_book["valid"] = False
                micro["last_error"] = f"{type(exc).__name__}: {str(exc)}"[:500]
                micro["reconnects"] += 1
                await self.persist()
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
            finally:
                micro["connected"] = False
                micro["book_valid"] = False
                self.micro_book["valid"] = False

        # Dispatch once per completed UTC minute; coalesce all instruments.
        if channel == "candle1m" and GITHUB_TOKEN:
            latest = max(parsed, key=lambda item: int(item["ts_ms"]))
            minute_key = int(latest["ts_ms"]) // 60000
            if self.last_bridge_dispatch_minute != minute_key:
                self.last_bridge_dispatch_minute = minute_key
                asyncio.create_task(dispatch_bridge_refresh(latest["completed_at"]))

    async def bootstrap_history(self, active: list[dict[str, str]]) -> None:
        """Warm 300 completed 1m/5m candles for all active markets and 1H/4H for exact ZEC X-Perp."""
        xperp = next((x for x in active if x.get("instId") == EXPECTED_XPERP_INST_ID), None)
        if xperp is None:
            raise RuntimeError(
                f"Expected exact EEA instrument {EXPECTED_XPERP_INST_ID} not found; "
                "refusing to substitute another contract"
            )

        # OKX history-candles uses at most 100 rows per request. Fetch older
        # pages with the documented "after" cursor and retain only completed,
        # ordered, deduplicated candles. Serialize requests to avoid bursts that
        # trigger regional HTTP 429 rate limits; retry transient 429/5xx responses.
        history_request_gate = asyncio.Semaphore(1)

        async def load(inst_id: str, stream_key: str, bar_name: str, interval: str) -> None:
            parse_bar = {"1m": "candle1m", "5m": "candle5m"}.get(interval, interval)
            collected: dict[int, dict[str, Any]] = {}
            cursor: int | None = None
            for _page in range(HISTORY_MAX_PAGES):
                params = {
                    "instId": inst_id,
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
                            f"OKX history {interval} {inst_id} HTTP {status} "
                            f"API {payload.get('code')} {payload.get('msg')}"
                        )
                    return payload

                payload = None
                for attempt in range(5):
                    try:
                        async with history_request_gate:
                            await asyncio.sleep(0.35)
                            payload = await asyncio.to_thread(fetch)
                        break
                    except urllib.error.HTTPError as request_error:
                        if request_error.code not in (429, 500, 502, 503, 504) or attempt == 4:
                            raise
                        retry_after = None
                        try:
                            retry_after = float(request_error.headers.get("Retry-After", ""))
                        except (TypeError, ValueError, AttributeError):
                            pass
                        wait_seconds = retry_after if retry_after is not None else min(2 ** attempt, 16)
                        await asyncio.sleep(max(1.0, wait_seconds))
                if payload is None:
                    raise RuntimeError(f"Empty OKX history response for {interval} {inst_id}")
                rows = payload.get("data", [])
                if not isinstance(rows, list) or not rows:
                    break
                now_ms = int(utc_now().timestamp() * 1000)
                for row in rows:
                    candle = parse_candle(row, parse_bar)
                    if candle is None:
                        continue
                    if int(candle["ts_ms"]) + BAR_SECONDS[parse_bar] * 1000 > now_ms:
                        continue
                    collected[int(candle["ts_ms"])] = candle
                try:
                    oldest = min(int(row[0]) for row in rows if isinstance(row, (list, tuple)) and row)
                except (TypeError, ValueError):
                    break
                if cursor is not None and oldest >= cursor:
                    break
                cursor = oldest
                if len(collected) >= HISTORY_TARGET_CANDLES:
                    break

            parsed = [collected[key] for key in sorted(collected)][-MAX_CANDLES:]
            if len(parsed) < 200:
                raise RuntimeError(
                    f"Only {len(parsed)} completed valid {interval} candles for {inst_id}; need at least 200"
                )
            expected_ms = BAR_SECONDS[parse_bar] * 1000
            gaps = sum(
                1 for previous, current in zip(parsed, parsed[1:])
                if int(current["ts_ms"]) - int(previous["ts_ms"]) != expected_ms
            )
            if gaps:
                raise RuntimeError(f"{interval} {inst_id} history contains {gaps} gap(s); refusing to mark history ready")
            async with self.lock:
                merge_candles(self.state["instruments"][stream_key][bar_name], parsed)
                await self.persist()

        jobs = []
        for item in active:
            exchange_id = item["instId"]
            stream_key = item.get("stream_key", exchange_id)
            for interval in ("1m", "5m"):
                jobs.append(load(exchange_id, stream_key, interval, interval))
        jobs.extend([
            load(EXPECTED_XPERP_INST_ID, "ZEC-USD_UM_XPERP", "1H", "1H"),
            load(EXPECTED_XPERP_INST_ID, "ZEC-USD_UM_XPERP", "4H", "4H"),
        ])
        results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [f"{type(result).__name__}: {result}" for result in results if isinstance(result, Exception)]
        if errors:
            self.state["service"]["last_error"] = ("History bootstrap: " + "; ".join(errors))[:500]
            await self.persist()
            raise RuntimeError("; ".join(errors))
        self.state["service"]["last_error"] = None

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
    micro_task = asyncio.create_task(feed.run_microstructure_websocket())
    print(f"Read-only OKX candle feed listening on 0.0.0.0:{port}", flush=True)
    try:
        async with server:
            await feed.stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        feed.stop_event.set()
        ws_task.cancel()
        micro_task.cancel()
        await asyncio.gather(ws_task, micro_task, return_exceptions=True)

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
