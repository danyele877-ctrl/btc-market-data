#!/usr/bin/env python3
"""Build a compact, machine-readable radar snapshot from the verified bridge files."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("data")
FEED = ROOT / "render_feed.json"
MICRO = ROOT / "zec_microstructure_history.json"
HISTORY = ROOT / "zec_xperp_history.json"
OUT = ROOT / "radar_snapshot.json"

def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}"}

def first_payload(record):
    payload = record.get("payload") if isinstance(record, dict) else None
    rows = payload.get("data") if isinstance(payload, dict) else None
    return rows[0] if isinstance(rows, list) and rows else None

def candle_tail(feed, key, bar, count=8):
    try:
        rows = feed["candles"]["payload"]["instruments"][key][bar]
        return rows[-count:] if isinstance(rows, list) else []
    except Exception:
        return []

def main():
    feed, micro, hist = read(FEED), read(MICRO), read(HISTORY)
    market = feed.get("okx_market", {}) if isinstance(feed, dict) else {}
    hist_bars = hist.get("bars", {}) if isinstance(hist, dict) else {}
    candles = {}
    for key in ("BTC-USDC", "ZEC-USDC", "ZEC-USD_UM_XPERP"):
        candles[key] = {}
        for bar in ("1m", "5m"):
            tail = candle_tail(feed, key, bar)
            candles[key][bar] = {
                "tail_count": len(tail),
                "latest": tail[-1] if tail else None,
                "previous": tail[-2] if len(tail) > 1 else None,
            }
    now = datetime.now(timezone.utc)
    def age(ts):
        if not ts:
            return None
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return round((now - parsed).total_seconds(), 1)
        except Exception:
            return None
    market_out = {}
    for name, record in market.items():
        market_out[name] = {
            "http_status": record.get("http_status"),
            "retrieved_at": record.get("retrieved_at"),
            "age_seconds": age(record.get("retrieved_at")),
            "error": record.get("error"),
            "first": first_payload(record),
        }
    history_out = {}
    for bar, item in hist_bars.items():
        history_out[bar] = {k: item.get(k) for k in (
            "count", "gap_count", "ready_200", "oldest_completed_at",
            "newest_completed_at", "error"
        )}
    books = micro.get("book_snapshots", []) if isinstance(micro, dict) else []
    trades = micro.get("trades", []) if isinstance(micro, dict) else []
    micro_out = {
        "schema_version": micro.get("schema_version"),
        "instrument": micro.get("instrument"),
        "book_snapshot_count": len(books),
        "last_book": books[-1] if books else None,
        "previous_book": books[-2] if len(books) > 1 else None,
        "trade_count": len(trades),
        "unique_trade_ids": len({str(x.get("tradeId")) for x in trades}),
        "first_trade": trades[0] if trades else None,
        "last_trade": trades[-1] if trades else None,
        "trade_window_start": trades[0].get("ts_ms") if trades else None,
    }
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_captured_at": feed.get("captured_at"),
        "source_status": feed.get("status"),
        "render_health_http": (feed.get("healthz") or {}).get("http_status"),
        "render_candles_http": (feed.get("candles") or {}).get("http_status"),
        "render_health": (feed.get("healthz") or {}).get("payload"),
        "market": market_out,
        "candles_1m_5m": candles,
        "zec_xperp_history": history_out,
        "microstructure": micro_out,
        "read_errors": {
            "feed": feed.get("_read_error"),
            "microstructure": micro.get("_read_error"),
            "history": hist.get("_read_error"),
        },
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUT),
        "bytes": OUT.stat().st_size,
        "source_status": output["source_status"],
        "market_records": len(market_out),
        "history_bars": len(history_out),
        "book_snapshots": micro_out["book_snapshot_count"],
        "trades": micro_out["trade_count"],
    }))

if __name__ == "__main__":
    main()
