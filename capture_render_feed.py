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

def trim_arrays(value, limit=320):
    if isinstance(value, dict):
        return {key: trim_arrays(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        # Candle arrays are lists of objects containing OHLC/timestamps.
        if value and all(isinstance(x, dict) and ("timestamp" in x or "ts_ms" in x) for x in value):
            return [trim_arrays(x, limit) for x in value[-limit:]]
        return [trim_arrays(x, limit) for x in value]
    return value

def main():
    health = get("/healthz")
    candles = get("/candles.json")
    if "payload" in candles:
        candles["payload"] = trim_arrays(candles["payload"])
    health_ok = health.get("http_status") == 200 and isinstance(health.get("payload"), dict)
    candles_ok = candles.get("http_status") == 200 and isinstance(candles.get("payload"), dict)
    status = "ok" if health_ok and candles_ok else "partial" if health_ok or candles_ok else "error"
    output = {
        "bridge_schema_version": 1,
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
