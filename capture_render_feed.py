#!/usr/bin/env python3
# Recovery probe: this file change triggers the Render bridge capture workflow.
"""Capture the public Render candle feed into a GitHub-readable snapshot."""
import json
import math
import os
import sys
import tempfile
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://btc-market-data-candles.onrender.com"
TIMEOUT = 20
OUT = Path("data/render_feed.json")
MICRO_OUT = Path("data/zec_microstructure_history.json")
MICRO_MAX_MINUTES = 1440
MICRO_MAX_TRADES = 30000
MICRO_MAX_BOOK_SNAPSHOTS = 15000
MAX_FEED_AGE_SECONDS = 20 * 60
HISTORY_MAX_AGE_SECONDS = {"1m": 180, "5m": 360, "1H": 3 * 3600, "4H": 12 * 3600}

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_feed(feed, current_time=None):
    """Reject stale, incomplete or internally inconsistent Render/OKX history."""
    current_time = current_time or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    problems = []

    def parse_time(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    if not isinstance(feed, dict):
        return ["Render bridge payload is not a JSON object"]
    captured = parse_time(feed.get("captured_at"))
    if captured is None or (current_time - captured).total_seconds() > MAX_FEED_AGE_SECONDS or captured > current_time:
        problems.append("captured_at missing, in the future, or older than 20 minutes")
    if feed.get("status") != "ok":
        problems.append("Render feed status is not ok")
    health = feed.get("healthz", {})
    candles = feed.get("candles", {})
    if not isinstance(health, dict):
        problems.append("Render health response record is malformed")
        health = {}
    if not isinstance(candles, dict):
        problems.append("Render candles response record is malformed")
        candles = {}
    if health.get("http_status") != 200 or not isinstance(health.get("payload"), dict):
        problems.append("Render health response is missing or not HTTP 200")
    if candles.get("http_status") != 200 or not isinstance(candles.get("payload"), dict):
        problems.append("Render candles response is missing or not HTTP 200")
    if captured is None:
        return problems + ["captured_at is required to validate completed candle freshness"]

    health_payload = health.get("payload") if isinstance(health.get("payload"), dict) else {}
    if health_payload.get("status") != "ok" or health_payload.get("websocket_connected") is not True:
        problems.append("Render service or candle WebSocket is not healthy")
    try:
        message_age = float(health_payload.get("last_message_age_seconds"))
    except (TypeError, ValueError):
        message_age = None
    if message_age is None or message_age < 0 or message_age > 120:
        problems.append("Render candle WebSocket message is missing, future-dated or older than 120 seconds")

    instruments = (candles.get("payload") or {}).get("instruments", {}) if isinstance(candles.get("payload"), dict) else {}
    streams = health_payload.get("streams", {}) if isinstance(health_payload.get("streams"), dict) else {}
    active = health_payload.get("active_instruments", [])
    expected = (
        ("BTC-USDC", "BTC-USDC", "BTC-USDC", "SPOT"),
        ("ZEC-USDC", "ZEC-USDC", "ZEC-USDC", "SPOT"),
        ("ZEC-USD_UM_XPERP", "ZEC-USD_UM_XPERP", "ZEC-USD_UM_XPERP-310530", "FUTURES"),
    )

    def validate_series(series, market, bar, stream_key):
        interval_seconds = {"1m": 60, "5m": 300}[bar]
        interval_ms = interval_seconds * 1000
        freshness_limit = {"1m": 180, "5m": 360}[bar]
        label = f"{market} {bar}"
        if not isinstance(series, list) or len(series) < 200:
            problems.append(f"Render {label} has fewer than 200 completed candles")
            return
        timestamps, completions = [], []
        try:
            for item in series:
                ts_ms = int(item["ts_ms"])
                values = {name: float(item[name]) for name in ("open", "high", "low", "close")}
                if not all(math.isfinite(value) for value in values.values()):
                    raise ValueError("non-finite OHLC")
                if values["low"] <= 0 or values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                    raise ValueError("inconsistent OHLC range")
                if ts_ms % interval_ms:
                    raise ValueError("timestamp is not aligned to timeframe")
                completed = parse_time(item.get("completed_at"))
                if completed is None:
                    raise ValueError("completed_at missing")
                completed_ms = int(completed.timestamp() * 1000)
                if abs(completed_ms - (ts_ms + interval_ms)) > 1000:
                    raise ValueError("completed_at inconsistent with timeframe")
                if completed > captured or completed > current_time:
                    raise ValueError("future or incomplete candle")
                timestamps.append(ts_ms)
                completions.append(completed)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"Render {label} contains malformed/open candle: {exc}")
            return
        if timestamps != sorted(set(timestamps)):
            problems.append(f"Render {label} is unordered or contains duplicate timestamps")
            return
        if any(right - left != interval_ms for left, right in zip(timestamps, timestamps[1:])):
            problems.append(f"Render {label} contains one or more candle gaps")
            return
        age = (captured - completions[-1]).total_seconds()
        if age < 0 or age > freshness_limit:
            problems.append(f"Render {label} latest completed candle is stale ({age:.1f}s; limit {freshness_limit}s)")
        stream = streams.get(f"{stream_key}:{bar}", {})
        try:
            stream_count = int(stream.get("candle_count") or 0)
            stream_gaps = int(stream.get("gap_count") or 0)
        except (TypeError, ValueError):
            stream_count, stream_gaps = 0, -1
        if (
            not stream or stream.get("stale") is True
            or stream.get("history_ready_200") is not True
            or stream_count < 200 or stream_gaps != 0
        ):
            problems.append(f"Render health stream {stream_key}:{bar} is stale, gapped or below 200 bars")
        else:
            health_completed = parse_time(stream.get("last_completed_at"))
            if health_completed is None or abs((health_completed - completions[-1]).total_seconds()) > 1:
                problems.append(f"Render health/candles timestamp mismatch for {stream_key}:{bar}")

    if isinstance(active, list):
        for _feed_key, stream_key, inst_id, inst_type in expected:
            if not any(
                isinstance(item, dict)
                and item.get("instId") == inst_id
                and item.get("instType") == inst_type
                and item.get("stream_key", item.get("instId")) == stream_key
                for item in active
            ):
                problems.append(f"Render active instrument missing or wrong: {inst_id}")
    else:
        problems.append("Render active_instruments is missing or malformed")

    for feed_key, stream_key, _inst_id, _inst_type in expected:
        market_series = instruments.get(feed_key, {}) if isinstance(instruments, dict) else {}
        for bar in ("1m", "5m"):
            validate_series(market_series.get(bar, []), feed_key, bar, stream_key)

    okx_history = feed.get("okx_history", {})
    if not isinstance(okx_history, dict):
        problems.append("OKX spot history is missing or malformed")
        okx_history = {}
    for market in ("BTC-USDC", "ZEC-USDC"):
        for bar in ("1H", "4H"):
            market_history = okx_history.get(market, {})
            if not isinstance(market_history, dict):
                problems.append(f"{market} spot history is missing or malformed")
                continue
            record = market_history.get(bar, {})
            if not isinstance(record, dict):
                problems.append(f"{market} {bar} history record is malformed")
                continue
            series = record.get("candles", [])
            label = f"{market} {bar}"
            if record.get("http_status") != 200 or not isinstance(series, list) or len(series) < 200:
                problems.append(f"{label} history has fewer than 200 candles or HTTP status is not 200")
                continue
            interval_ms = {"1H": 3_600_000, "4H": 14_400_000}[bar]
            timestamps = []
            try:
                for item in series:
                    ts_ms = int(item["ts_ms"])
                    values = {name: float(item[name]) for name in ("open", "high", "low", "close")}
                    if not all(math.isfinite(value) for value in values.values()):
                        raise ValueError("non-finite OHLC")
                    if values["low"] <= 0 or values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                        raise ValueError("inconsistent OHLC range")
                    if ts_ms % interval_ms:
                        raise ValueError("timestamp is not aligned to timeframe")
                    completed = parse_time(item.get("completed_at"))
                    if completed is None or abs(int(completed.timestamp() * 1000) - (ts_ms + interval_ms)) > 1000:
                        raise ValueError("completed_at inconsistent")
                    if completed > captured or completed > current_time:
                        raise ValueError("future/incomplete candle")
                    timestamps.append(ts_ms)
            except (KeyError, TypeError, ValueError):
                problems.append(f"{label} history contains malformed or incomplete candles")
                continue
            if timestamps != sorted(set(timestamps)) or any(
                right - left != interval_ms for left, right in zip(timestamps, timestamps[1:])
            ):
                problems.append(f"{label} history is unordered, duplicated, or has gaps")
            newest = parse_time(series[-1].get("completed_at")) if series and isinstance(series[-1], dict) else None
            if newest is None or (captured - newest).total_seconds() > HISTORY_MAX_AGE_SECONDS[bar] or newest > captured:
                problems.append(f"{label} history is missing or stale")

    xperp_history = feed.get("zec_xperp_history", {})
    if not isinstance(xperp_history, dict):
        problems.append("ZEC X-Perp history is missing or malformed")
        xperp_history = {}
    if xperp_history.get("instrument") != "ZEC-USD_UM_XPERP-310530":
        problems.append("ZEC X-Perp history instrument is not the exact OKX Europe contract")
    xperp_bars = xperp_history.get("bars", {})
    if not isinstance(xperp_bars, dict):
        problems.append("ZEC X-Perp history bars are missing or malformed")
        xperp_bars = {}
    for bar in ("1m", "5m", "1H", "4H"):
        record = xperp_bars.get(bar, {})
        if not isinstance(record, dict):
            problems.append(f"ZEC-USD_UM_XPERP-310530 {bar} history record is malformed")
            continue
        label = f"ZEC-USD_UM_XPERP-310530 {bar} history"
        try:
            count = int(record.get("count") or 0)
            gaps = int(record.get("gap_count") or 0)
        except (TypeError, ValueError):
            count, gaps = 0, -1
        if (
            record.get("http_status") != 200 or record.get("ready_200") is not True
            or count < 200 or gaps != 0 or record.get("error")
        ):
            problems.append(f"{label} is not ready (HTTP 200, 200 bars, zero gaps required)")
            continue
        newest = parse_time(record.get("newest_completed_at"))
        if newest is None or newest > captured or newest > current_time:
            problems.append(f"{label} has invalid/future newest completed timestamp")
            continue
        age = (captured - newest).total_seconds()
        if age < 0 or age > HISTORY_MAX_AGE_SECONDS[bar]:
            problems.append(f"{label} is stale ({age:.1f}s; limit {HISTORY_MAX_AGE_SECONDS[bar]}s)")
    return problems

def write_json_atomically(path, payload):
    """Write a complete JSON document before replacing the last valid file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_feed_atomically(feed):
    write_json_atomically(OUT, feed)

def get(path):
    url = BASE + path
    started = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": "btc-market-data-github-bridge/1.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            return {
                "http_status": response.status,
                "retrieved_at": now(),
                "latency_ms": round((time.time() - started) * 1000),
                "payload": payload,
            }
    except Exception as exc:
        return {
            "http_status": None,
            "retrieved_at": now(),
            "latency_ms": round((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }

def get_url(url):
    """Fetch a public JSON endpoint and retain its own source timestamp."""
    started = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": "btc-market-data-github-bridge/1.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {
                "http_status": response.status,
                "retrieved_at": now(),
                "latency_ms": round((time.time() - started) * 1000),
                "endpoint": url,
                "payload": payload,
            }
    except Exception as exc:
        return {
            "http_status": None,
            "retrieved_at": now(),
            "latency_ms": round((time.time() - started) * 1000),
            "endpoint": url,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }

def trim_arrays(value, limit=500):
    if isinstance(value, dict):
        return {key: trim_arrays(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        # Candle arrays are lists of objects containing OHLC/timestamps.
        if value and all(isinstance(x, dict) and ("timestamp" in x or "ts_ms" in x) for x in value):
            return [trim_arrays(x, limit) for x in value[-limit:]]
        return [trim_arrays(x, limit) for x in value]
    return value


OKX_EEA = "https://eea.okx.com/api/v5"
HISTORY_MARKETS = {
    "BTC-USDC": "BTC-USDC",
    "ZEC-USDC": "ZEC-USDC",
    "ZEC-USD_UM_XPERP": "ZEC-USD_UM_XPERP-310530",
}

def get_okx_history(inst_id, bar, limit=300):
    url = f"{OKX_EEA}/market/history-candles?instId={inst_id}&bar={bar}&limit={limit}"
    started = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": "btc-market-data-github-bridge/1.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if response.status != 200 or payload.get("code") != "0":
                raise RuntimeError(f"HTTP {response.status}; OKX code={payload.get('code')} msg={payload.get('msg')}")
            seconds = {"1H": 3600, "4H": 14400}[bar]
            complete = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 9 or str(row[8]) != "1":
                    continue
                ts = int(row[0])
                complete.append({
                    "ts_ms": ts,
                    "timestamp": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "completed_at": datetime.fromtimestamp(ts / 1000 + seconds, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "open": row[1], "high": row[2], "low": row[3], "close": row[4],
                    "volume": row[5], "volume_currency": row[6], "volume_quote": row[7],
                })
            complete.sort(key=lambda x: x["ts_ms"])
            return {
                "http_status": response.status,
                "retrieved_at": now(),
                "latency_ms": round((time.time() - started) * 1000),
                "instId": inst_id,
                "bar": bar,
                "count": len(complete),
                "candles": complete[-limit:],
            }
    except Exception as exc:
        return {
            "http_status": None,
            "retrieved_at": now(),
            "latency_ms": round((time.time() - started) * 1000),
            "instId": inst_id,
            "bar": bar,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "candles": [],
        }

def get_okx_xperp_history(inst_id, bars=("1m", "5m", "1H", "4H"), target=250):
    """Fetch completed historical candles for the exact ZEC X-Perp, paginating older rows."""
    seconds = {"1m": 60, "5m": 300, "1H": 3600, "4H": 14400}
    output = {"schema_version": 1, "instrument": inst_id, "retrieved_at": now(), "bars": {}}
    for bar in bars:
        collected = {}
        cursor = None
        errors = []
        response_statuses = []
        for _ in range(4):
            params = {"instId": inst_id, "bar": bar, "limit": "100"}
            if cursor is not None:
                params["after"] = str(cursor)
            url = f"{OKX_EEA}/market/history-candles?" + urllib.parse.urlencode(params)
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "btc-market-data-github-bridge/1.0",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                })
                with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    response_statuses.append(response.status)
                    if response.status != 200 or payload.get("code") != "0":
                        raise RuntimeError(f"HTTP {response.status}; OKX {payload.get('code')} {payload.get('msg')}")
                rows = payload.get("data", [])
                if not rows:
                    break
                current_ms = int(time.time() * 1000)
                for row in rows:
                    if not isinstance(row, list) or len(row) < 9 or str(row[8]) != "1":
                        continue
                    ts = int(row[0])
                    if ts + seconds[bar] * 1000 > current_ms:
                        continue
                    collected[ts] = {
                        "ts_ms": ts,
                        "timestamp": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                        "completed_at": datetime.fromtimestamp(ts / 1000 + seconds[bar], timezone.utc).isoformat().replace("+00:00", "Z"),
                        "open": row[1], "high": row[2], "low": row[3], "close": row[4],
                        "volume": row[5], "volume_currency": row[6], "volume_quote": row[7],
                    }
                oldest = min(int(row[0]) for row in rows if isinstance(row, list) and row)
                if cursor is not None and oldest >= cursor:
                    break
                cursor = oldest
                if len(collected) >= target:
                    break
                time.sleep(0.12)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}"[:250])
                break
        candles = [collected[key] for key in sorted(collected)][-target:]
        gaps = sum(1 for a, b in zip(candles, candles[1:]) if b["ts_ms"] - a["ts_ms"] != seconds[bar] * 1000)
        http_ok = bool(response_statuses) and all(status == 200 for status in response_statuses) and not errors
        output["bars"][bar] = {
            "http_status": 200 if http_ok else next((status for status in response_statuses if status != 200), None),
            "count": len(candles), "gap_count": gaps,
            "ready_200": http_ok and len(candles) >= 200 and gaps == 0,
            "oldest_completed_at": candles[0]["completed_at"] if candles else None,
            "newest_completed_at": candles[-1]["completed_at"] if candles else None,
            "error": "; ".join(errors) if errors else None,
            "candles": candles,
        }
    return output

def load_microstructure_history():
    try:
        value = json.loads(MICRO_OUT.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema_version") == 1:
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"schema_version": 1, "instrument": "ZEC-USD_UM_XPERP-310530", "book_snapshots": [], "trades": [], "open_interest_history": [], "capture_status": []}

def update_microstructure_history(okx_market, websocket_micro=None, captured_at=None):
    websocket_micro = websocket_micro or {}
    history = load_microstructure_history()
    history.setdefault("open_interest_history", [])
    history.setdefault("capture_status", [])
    book_result = okx_market.get("zec_xperp_book", {})
    book_payload = book_result.get("payload", {})
    book_rows = book_payload.get("data", []) if isinstance(book_payload, dict) else []
    if book_result.get("http_status") == 200 and book_rows:
        row = book_rows[0]
        ts = int(row.get("ts", 0) or 0)
        if ts:
            history["book_snapshots"].append({
                "ts_ms": ts,
                "captured_at": book_result.get("retrieved_at"),
                "source": "rest",
                "bids": row.get("bids", [])[:20],
                "asks": row.get("asks", [])[:20],
            })
    # Preserve sequenced WebSocket top-10 depth states.
    for row in websocket_micro.get("book_depth_history", []) or []:
        if not isinstance(row, dict):
            continue
        try:
            ts, seq_id = int(row.get("ts_ms", 0)), int(row.get("seq_id"))
        except (TypeError, ValueError):
            continue
        if ts and row.get("bids") is not None and row.get("asks") is not None:
            history["book_snapshots"].append({
                "ts_ms": ts, "captured_at": None, "source": "websocket",
                "seq_id": seq_id, "bids": row.get("bids", [])[:10],
                "asks": row.get("asks", [])[:10],
            })
    # Pair OI with the exact X-Perp ticker's USD price at capture time. This is
    # a contract-count context series only; never convert it to ZEC exposure.
    oi_result = okx_market.get("zec_xperp_open_interest", {})
    oi_payload = oi_result.get("payload", {})
    oi_rows = oi_payload.get("data", []) if isinstance(oi_payload, dict) else []
    ticker_result = okx_market.get("zec_xperp_ticker", {})
    ticker_payload = ticker_result.get("payload", {})
    ticker_rows = ticker_payload.get("data", []) if isinstance(ticker_payload, dict) else []
    if oi_result.get("http_status") == 200 and oi_rows and ticker_rows:
        oi_row, ticker = oi_rows[0], ticker_rows[0]
        try:
            ts_ms = int(oi_row.get("ts", 0) or ticker.get("ts", 0))
            oi_contracts = float(oi_row.get("oi", 0))
            price_usd = float(ticker.get("last", 0))
        except (TypeError, ValueError):
            ts_ms, oi_contracts, price_usd = 0, 0, 0
        if ts_ms and oi_contracts > 0 and price_usd > 0:
            history["open_interest_history"].append({
                "ts_ms": ts_ms,
                "captured_at": oi_result.get("retrieved_at"),
                "oi_contracts": str(oi_contracts),
                "price_usd": str(price_usd),
                "inst_id": "ZEC-USD_UM_XPERP-310530",
                "unit": "contracts",
            })
    trade_result = okx_market.get("zec_xperp_trades", {})
    trade_payload = trade_result.get("payload", {})
    trade_rows = trade_payload.get("data", []) if isinstance(trade_payload, dict) else []
    known = {str(x.get("tradeId")): i for i, x in enumerate(history["trades"])}
    if trade_result.get("http_status") == 200:
        for row in trade_rows:
            trade_id = str(row.get("tradeId", ""))
            try:
                ts = int(row.get("ts", 0))
                size = float(row.get("sz", 0))
            except (TypeError, ValueError):
                continue
            if not trade_id or size <= 0 or row.get("side") not in ("buy", "sell"):
                continue
            item = {
                "tradeId": trade_id, "ts_ms": ts, "side": row["side"],
                "sz": str(size), "px": str(row.get("px", "")), "source": "rest",
            }
            if trade_id not in known:
                known[trade_id] = len(history["trades"])
                history["trades"].append(item)
    # Merge the live WS tape by trade ID, preferring the richer sequenced record.
    for row in websocket_micro.get("trade_events", []) or []:
        if not isinstance(row, dict):
            continue
        trade_id = str(row.get("trade_id", ""))
        try:
            ts, size = int(row.get("ts_ms", 0)), float(row.get("size_contracts", 0))
        except (TypeError, ValueError):
            continue
        if not trade_id or ts <= 0 or size <= 0 or row.get("side") not in ("buy", "sell"):
            continue
        item = {
            "tradeId": trade_id, "ts_ms": ts, "side": row["side"],
            "sz": str(size), "px": str(row.get("price", "")),
            "count": int(row.get("count", 1)), "source": "websocket",
            "gap_before": bool(row.get("gap_before")),
        }
        if trade_id in known:
            index = known[trade_id]
            if history["trades"][index].get("source") != "websocket":
                history["trades"][index] = item
        else:
            known[trade_id] = len(history["trades"])
            history["trades"].append(item)
    book_by_key = {}
    for row in history["book_snapshots"]:
        book_by_key[(row.get("source", "rest"), int(row.get("ts_ms", 0)))] = row
    history["book_snapshots"] = sorted(book_by_key.values(), key=lambda x: x["ts_ms"])
    history["trades"].sort(key=lambda x: (x["ts_ms"], x["tradeId"]))
    history["open_interest_history"] = sorted(
        {int(x["ts_ms"]): x for x in history["open_interest_history"] if x.get("ts_ms")}.values(),
        key=lambda x: x["ts_ms"],
    )
    cutoff = int(time.time() * 1000) - MICRO_MAX_MINUTES * 60 * 1000
    history["book_snapshots"] = [x for x in history["book_snapshots"] if x["ts_ms"] >= cutoff][-MICRO_MAX_BOOK_SNAPSHOTS:]
    history["trades"] = [x for x in history["trades"] if x["ts_ms"] >= cutoff][-MICRO_MAX_TRADES:]
    history["open_interest_history"] = [x for x in history["open_interest_history"] if x["ts_ms"] >= cutoff]
    history["capture_status"].append({
        "captured_at": captured_at or websocket_micro.get("last_message_at") or now(),
        "trade_events_returned": int(websocket_micro.get("trade_events_returned") or 0),
        "trade_events_truncated": bool(websocket_micro.get("trade_events_truncated")),
        "trade_gap_count": int(websocket_micro.get("trade_gap_count") or 0),
        "book_gap_count": int(websocket_micro.get("book_gap_count") or 0),
        "book_valid": bool(websocket_micro.get("book_valid")),
        "trade_age_seconds": websocket_micro.get("trade_age_seconds"),
        "book_age_seconds": websocket_micro.get("book_age_seconds"),
    })
    history["capture_status"] = history["capture_status"][-MICRO_MAX_MINUTES:]
    MICRO_OUT.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(MICRO_OUT, history)
    return {
        "book_snapshots": len(history["book_snapshots"]),
        "websocket_book_snapshots": sum(1 for x in history["book_snapshots"] if x.get("source") == "websocket"),
        "unique_trades": len(history["trades"]),
        "websocket_trades": sum(1 for x in history["trades"] if x.get("source") == "websocket"),
        "websocket_trade_gaps": sum(1 for x in history["trades"] if x.get("source") == "websocket" and x.get("gap_before")),
        "oldest_trade_at": datetime.fromtimestamp(history["trades"][0]["ts_ms"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if history["trades"] else None,
        "newest_trade_at": datetime.fromtimestamp(history["trades"][-1]["ts_ms"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if history["trades"] else None,
    }

def main():
    if sys.argv[1:] == ["--validate-existing"]:
        try:
            current = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid Render feed file: {exc}")
        problems = validate_feed(current)
        if problems:
            raise SystemExit("Render feed validation failed: " + "; ".join(problems))
        print(f"Render feed validation passed: {OUT}")
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: python capture_render_feed.py [--validate-existing]")

    health = get("/healthz")
    candles = get("/candles.json")
    if "payload" in candles:
        candles["payload"] = trim_arrays(candles["payload"])
    health_ok = health.get("http_status") == 200 and isinstance(health.get("payload"), dict)
    candles_ok = candles.get("http_status") == 200 and isinstance(candles.get("payload"), dict)
    status = "ok" if health_ok and candles_ok else "partial" if health_ok or candles_ok else "error"
    okx_history = {
        market: {bar: get_okx_history(inst_id, bar) for bar in ("1H", "4H")}
        for market, inst_id in HISTORY_MARKETS.items()
    }
    # Capture short-lived derivatives context in the same fast bridge snapshot.
    # These are independent public OKX EEA requests, each with its own retrieval time.
    market_paths = {
        "btc_spot_ticker": "/market/ticker?instId=BTC-USDC",
        "zec_spot_ticker": "/market/ticker?instId=ZEC-USDC",
        "zec_xperp_ticker": "/market/ticker?instId=ZEC-USD_UM_XPERP-310530",
        "zec_xperp_book": "/market/books?instId=ZEC-USD_UM_XPERP-310530&sz=50",
        "zec_xperp_trades": "/market/trades?instId=ZEC-USD_UM_XPERP-310530&limit=100",
        "zec_xperp_mark": "/public/mark-price?instType=FUTURES&instId=ZEC-USD_UM_XPERP-310530",
        "zec_index": "/market/index-tickers?quoteCcy=USD",
        "zec_xperp_funding": "/public/funding-rate?instId=ZEC-USD_UM_XPERP-310530",
        "zec_xperp_open_interest": "/public/open-interest?instType=FUTURES&instId=ZEC-USD_UM_XPERP-310530",
        "zec_xperp_instrument": "/public/instruments?instType=FUTURES&instId=ZEC-USD_UM_XPERP-310530",
    }
    okx_market = {
        name: get_url(f"{OKX_EEA}{path}")
        for name, path in market_paths.items()
    }
    # Independent cross-region check for the exact same dated instrument.
    # This is diagnostic only: global metadata must never override EEA trading rules.
    okx_market["zec_xperp_instrument_global"] = get_url(
        "https://www.okx.com/api/v5/public/instruments?"
        "instType=FUTURES&instId=ZEC-USD_UM_XPERP-310530"
    )
    # Keep only the ZEC USD index row; never mix unrelated index instruments.
    index_payload = okx_market.get("zec_index", {}).get("payload")
    if isinstance(index_payload, dict) and isinstance(index_payload.get("data"), list):
        index_payload["data"] = [
            row for row in index_payload["data"]
            if "ZEC" in str(row.get("instId", "")).upper()
        ]
    microstructure_status = update_microstructure_history(
        okx_market,
        (health.get("payload") or {}).get("microstructure", {}),
        health.get("retrieved_at"),
    )
    # Raw WebSocket tape/depth have been persisted in the dedicated history;
    # keep the frequently committed bridge feed compact.
    health_payload = health.get("payload")
    if isinstance(health_payload, dict) and isinstance(health_payload.get("microstructure"), dict):
        health_payload["microstructure"].pop("trade_events", None)
        health_payload["microstructure"].pop("book_depth_history", None)
    zec_xperp_history = get_okx_xperp_history("ZEC-USD_UM_XPERP-310530")
    write_json_atomically(Path("data/zec_xperp_history.json"), zec_xperp_history)
    output = {
        "bridge_schema_version": 4,
        "microstructure_history": microstructure_status,
        "zec_xperp_history": {
            "instrument": zec_xperp_history.get("instrument"),
            "retrieved_at": zec_xperp_history.get("retrieved_at"),
            "bars": {bar: {key: value for key, value in item.items() if key != "candles"} for bar, item in zec_xperp_history["bars"].items()},
        },
        "okx_market": okx_market,
        "okx_history": okx_history,
        "captured_at": now(),
        "status": status,
        "healthz": health,
        "candles": candles,
    }
    problems = validate_feed(output)
    if problems:
        raise SystemExit("Refusing to replace the last known feed: " + "; ".join(problems))
    write_feed_atomically(output)
    print(json.dumps({
        "status": status,
        "health_http": health.get("http_status"),
        "candles_http": candles.get("http_status"),
        "health_error": health.get("error"),
        "candles_error": candles.get("error"),
        "output": str(OUT),
    }, ensure_ascii=False))
    # Do not fail the existing market-data collector if Render is temporarily unavailable.
    # The saved snapshot explicitly records the failure for the radar to classify as unconfirmed.

if __name__ == "__main__":
    main()
