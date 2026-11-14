#!/usr/bin/env python3
"""Capture the public Render candle feed into a GitHub-readable snapshot."""
import json
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

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

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

def trim_arrays(value, limit=320):
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
    return {"schema_version": 1, "instrument": "ZEC-USD_UM_XPERP-310530", "book_snapshots": [], "trades": [], "open_interest_history": []}

def update_microstructure_history(okx_market):
    history = load_microstructure_history()
    history.setdefault("open_interest_history", [])
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
                "bids": row.get("bids", [])[:20],
                "asks": row.get("asks", [])[:20],
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
    known = {str(x.get("tradeId")) for x in history["trades"]}
    if trade_result.get("http_status") == 200:
        for row in trade_rows:
            trade_id = str(row.get("tradeId", ""))
            try:
                ts = int(row.get("ts", 0))
                size = float(row.get("sz", 0))
            except (TypeError, ValueError):
                continue
            if not trade_id or trade_id in known or size <= 0 or row.get("side") not in ("buy", "sell"):
                continue
            history["trades"].append({
                "tradeId": trade_id,
                "ts_ms": ts,
                "side": row["side"],
                "sz": str(size),
                "px": str(row.get("px", "")),
            })
            known.add(trade_id)
    history["book_snapshots"].sort(key=lambda x: x["ts_ms"])
    history["trades"].sort(key=lambda x: (x["ts_ms"], x["tradeId"]))
    history["open_interest_history"] = sorted(
        {int(x["ts_ms"]): x for x in history["open_interest_history"] if x.get("ts_ms")}.values(),
        key=lambda x: x["ts_ms"],
    )
    cutoff = int(time.time() * 1000) - MICRO_MAX_MINUTES * 60 * 1000
    history["book_snapshots"] = [x for x in history["book_snapshots"] if x["ts_ms"] >= cutoff][-MICRO_MAX_MINUTES:]
    history["trades"] = [x for x in history["trades"] if x["ts_ms"] >= cutoff][-MICRO_MAX_TRADES:]
    history["open_interest_history"] = [x for x in history["open_interest_history"] if x["ts_ms"] >= cutoff]
    MICRO_OUT.parent.mkdir(parents=True, exist_ok=True)
    MICRO_OUT.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {
        "book_snapshots": len(history["book_snapshots"]),
        "unique_trades": len(history["trades"]),
        "oldest_trade_at": datetime.fromtimestamp(history["trades"][0]["ts_ms"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if history["trades"] else None,
        "newest_trade_at": datetime.fromtimestamp(history["trades"][-1]["ts_ms"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if history["trades"] else None,
    }

def main():
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
    # Keep only the ZEC USD index row; never mix unrelated index instruments.
    index_payload = okx_market.get("zec_index", {}).get("payload")
    if isinstance(index_payload, dict) and isinstance(index_payload.get("data"), list):
        index_payload["data"] = [
            row for row in index_payload["data"]
            if "ZEC" in str(row.get("instId", "")).upper()
        ]
    microstructure_status = update_microstructure_history(okx_market)
    zec_xperp_history = get_okx_xperp_history("ZEC-USD_UM_XPERP-310530")
    Path("data/zec_xperp_history.json").write_text(json.dumps(zec_xperp_history, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    output = {
        "bridge_schema_version": 4,
        "microstructure_history": microstructure_status,
        "zec_xperp_history": {bar: {key: value for key, value in item.items() if key != "candles"} for bar, item in zec_xperp_history["bars"].items()},
        "okx_market": okx_market,
        "okx_history": okx_history,
        "captured_at": now(),
        "status": status,
        "healthz": health,
        "candles": candles,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
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
