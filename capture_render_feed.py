#!/usr/bin/env python3
"""Capture the public Render candle feed into a GitHub-readable snapshot."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://btc-market-data-candles.onrender.com"
TIMEOUT = 20
OUT = Path("data/render_feed.json")

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
    output = {
        "bridge_schema_version": 3,
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
