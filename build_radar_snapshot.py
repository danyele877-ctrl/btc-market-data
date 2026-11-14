#!/usr/bin/env python3
"""Build a compact, machine-readable radar snapshot from verified bridge files."""
import json
from datetime import datetime, timezone
from pathlib import Path
from zec_early_signal import evaluate_signal

ROOT = Path("data")
FEED = ROOT / "render_feed.json"
MICRO = ROOT / "zec_microstructure_history.json"
HISTORY = ROOT / "zec_xperp_history.json"
OUT = ROOT / "radar_snapshot.json"
BAR_SECONDS = {"1m": 60, "5m": 300, "1H": 3600, "4H": 14400}


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


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def ema_series(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1.0 - alpha)
        result[index] = previous
    return result


def wilder_average(values, period):
    if len(values) < period:
        return None
    average = sum(values[:period]) / period
    for value in values[period:]:
        average = ((average * (period - 1)) + value) / period
    return average


def indicator_pack(candles):
    """Calculate common indicators from ordered, gap-free completed OHLC candles."""
    try:
        closes = [float(item["close"]) for item in candles]
        highs = [float(item["high"]) for item in candles]
        lows = [float(item["low"]) for item in candles]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) < 200:
        return None

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(value, 0.0) for value in changes]
    losses = [max(-value, 0.0) for value in changes]
    avg_gain = wilder_average(gains, 14)
    avg_loss = wilder_average(losses, 14)
    if avg_gain is None or avg_loss is None:
        rsi14 = None
    elif avg_loss == 0:
        rsi14 = 100.0 if avg_gain > 0 else 50.0
    else:
        rsi14 = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_values = [
        (ema12[i] - ema26[i]) if ema12[i] is not None and ema26[i] is not None else None
        for i in range(len(closes))
    ]
    first_macd = next((i for i, value in enumerate(macd_values) if value is not None), None)
    macd_line = macd_values[-1]
    signal_value = None
    histogram = None
    if first_macd is not None:
        compact_macd = [value for value in macd_values[first_macd:] if value is not None]
        signal_series = ema_series(compact_macd, 9)
        signal_value = signal_series[-1]
        if macd_line is not None and signal_value is not None:
            histogram = macd_line - signal_value

    true_ranges = []
    for i in range(1, len(closes)):
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr14 = wilder_average(true_ranges, 14)

    return {
        "ema20": round(ema20, 8) if ema20 is not None else None,
        "ema50": round(ema50, 8) if ema50 is not None else None,
        "ema200": round(ema200, 8) if ema200 is not None else None,
        "rsi14": round(rsi14, 4) if rsi14 is not None else None,
        "macd": round(macd_line, 8) if macd_line is not None else None,
        "macd_signal": round(signal_value, 8) if signal_value is not None else None,
        "macd_histogram": round(histogram, 8) if histogram is not None else None,
        "atr14": round(atr14, 8) if atr14 is not None else None,
    }


def summarize_history(record, bar, now):
    candles = record.get("candles", []) if isinstance(record, dict) else []
    candles = sorted(
        [item for item in candles if isinstance(item, dict) and item.get("ts_ms") is not None],
        key=lambda item: int(item["ts_ms"]),
    )
    interval = BAR_SECONDS.get(bar)
    gaps = sum(
        1 for previous, current in zip(candles, candles[1:])
        if interval and int(current["ts_ms"]) - int(previous["ts_ms"]) != interval * 1000
    )
    newest = candles[-1] if candles else None
    completed = parse_time(newest.get("completed_at")) if newest else None
    age = (now - completed).total_seconds() if completed else None
    http_ok = record.get("http_status") in (None, 200) and not record.get("error")
    ready = (
        http_ok and len(candles) >= 200 and gaps == 0
        and age is not None and -60 <= age <= (interval * 3 if interval else 0)
    )
    return {
        "http_status": record.get("http_status"),
        "count": len(candles),
        "gap_count": gaps,
        "ready_200": ready,
        "oldest_completed_at": candles[0].get("completed_at") if candles else None,
        "newest_completed_at": newest.get("completed_at") if newest else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "last_close": newest.get("close") if newest else None,
        "indicators": indicator_pack(candles) if ready else None,
        "error": record.get("error"),
    }


def microstructure_summary(micro):
    books = micro.get("book_snapshots", []) if isinstance(micro, dict) else []
    trades = micro.get("trades", []) if isinstance(micro, dict) else []
    sorted_trades = sorted(trades, key=lambda item: (int(item.get("ts_ms", 0)), int(item.get("tradeId", 0))))
    ids = sorted({int(item["tradeId"]) for item in sorted_trades if str(item.get("tradeId", "")).isdigit()})
    trade_gap_events = sum(1 for previous, current in zip(ids, ids[1:]) if current - previous > 1)
    sorted_books = sorted(books, key=lambda item: int(item.get("ts_ms", 0)))
    intervals = [
        (int(current["ts_ms"]) - int(previous["ts_ms"])) / 1000.0
        for previous, current in zip(sorted_books, sorted_books[1:])
        if int(current.get("ts_ms", 0)) > int(previous.get("ts_ms", 0))
    ]
    median_interval = None
    if intervals:
        ordered = sorted(intervals)
        middle = len(ordered) // 2
        median_interval = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    last_book = sorted_books[-1] if sorted_books else None
    last_trade = sorted_trades[-1] if sorted_trades else None
    oi_history = micro.get("open_interest_history", []) if isinstance(micro, dict) else []
    oi_history = [item for item in oi_history if isinstance(item, dict) and item.get("inst_id") == "ZEC-USD_UM_XPERP-310530"]
    return {
        "schema_version": micro.get("schema_version"),
        "instrument": micro.get("instrument"),
        "collection_method": "REST polling",
        "book_snapshot_count": len(books),
        "book_sample_median_seconds": round(median_interval, 1) if median_interval is not None else None,
        "last_book": last_book,
        "previous_book": sorted_books[-2] if len(sorted_books) > 1 else None,
        "last_book_captured_at": last_book.get("captured_at") if last_book else None,
        "trade_count": len(trades),
        "unique_trade_ids": len(ids),
        "trade_id_gap_events": trade_gap_events,
        "first_trade": sorted_trades[0] if sorted_trades else None,
        "last_trade": last_trade,
        "trade_window_start": sorted_trades[0].get("ts_ms") if sorted_trades else None,
        "trade_window_end": last_trade.get("ts_ms") if last_trade else None,
        "cvd_status": "N/D: REST polling with detected tradeId gaps" if trade_gap_events else "N/D: REST polling; continuity not proven",
        "book_sequence_status": "N/D: REST snapshots do not provide a continuous WS sequence chain",
        "open_interest_history": oi_history,
    }


def main():
    feed, micro, hist = read(FEED), read(MICRO), read(HISTORY)
    market = feed.get("okx_market", {}) if isinstance(feed, dict) else {}
    hist_bars = hist.get("bars", {}) if isinstance(hist, dict) else {}
    now = datetime.now(timezone.utc)

    candles = {}
    for key in ("BTC-USDC", "ZEC-USDC", "ZEC-USD_UM_XPERP"):
        candles[key] = {}
        for bar in ("1m", "5m"):
            tail = candle_tail(feed, key, bar)
            candles[key][bar] = {
                "tail_count": len(tail),
                "tail": tail[-8:],
                "latest": tail[-1] if tail else None,
                "previous": tail[-2] if len(tail) > 1 else None,
            }

    market_out = {}
    for name, record in market.items():
        first = first_payload(record)
        payload = record.get("payload") if isinstance(record, dict) else None
        exchange_ts = first.get("ts") if isinstance(first, dict) else None
        exchange_age = None
        try:
            exchange_age = round((now.timestamp() - int(exchange_ts) / 1000), 1) if exchange_ts else None
        except (TypeError, ValueError):
            pass
        market_out[name] = {
            "http_status": record.get("http_status"),
            "api_code": payload.get("code") if isinstance(payload, dict) else None,
            "retrieved_at": record.get("retrieved_at"),
            "age_seconds": (
                round((now - parse_time(record.get("retrieved_at"))).total_seconds(), 1)
                if parse_time(record.get("retrieved_at")) else None
            ),
            "exchange_timestamp": exchange_ts,
            "exchange_age_seconds": exchange_age,
            "error": record.get("error"),
            "first": first,
        }

    xperp_history = {
        bar: summarize_history(item, bar, now)
        for bar, item in hist_bars.items()
    }
    spot_history = {}
    okx_history = feed.get("okx_history", {}) if isinstance(feed, dict) else {}
    for instrument in ("BTC-USDC", "ZEC-USDC"):
        source = okx_history.get(instrument, {})
        spot_history[instrument] = {
            bar: summarize_history(source.get(bar, {}), bar, now)
            for bar in ("1H", "4H")
        }

    health_payload = (feed.get("healthz") or {}).get("payload", {})
    ws_micro = health_payload.get("microstructure", {}) if isinstance(health_payload, dict) else {}
    if not ws_micro:
        candle_payload = (feed.get("candles") or {}).get("payload", {})
        ws_micro = candle_payload.get("microstructure", {}) if isinstance(candle_payload, dict) else {}
    micro_out = microstructure_summary(micro)
    micro_out["websocket"] = ws_micro
    micro_out["cvd"] = ws_micro.get("cvd")
    micro_out["book_imbalance"] = (ws_micro.get("book") or {})
    if ws_micro:
        cvd = ws_micro.get("cvd") or {}
        cvd_ready_short = bool(cvd.get("ready_1m") and cvd.get("ready_5m"))
        cvd_ready_full = cvd_ready_short and bool(cvd.get("ready_15m"))
        if cvd_ready_full:
            micro_out["cvd_status"] = "OK: WebSocket continuo, delta in contratti su 1m/5m/15m"
        elif cvd_ready_short:
            micro_out["cvd_status"] = "PARZIALE: CVD 1m/5m pronto; finestra 15m in riscaldamento"
        else:
            micro_out["cvd_status"] = "N/D: WebSocket in riscaldamento o continuità non verificata"
        micro_out["book_sequence_status"] = (
            "OK: sequenza WebSocket valida"
            if ws_micro.get("book_valid") else "N/D: book non valido o in attesa di snapshot"
        )
    output = {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_captured_at": feed.get("captured_at"),
        "source_status": feed.get("status"),
        "render_health_http": (feed.get("healthz") or {}).get("http_status"),
        "render_candles_http": (feed.get("candles") or {}).get("http_status"),
        "render_health": (feed.get("healthz") or {}).get("payload"),
        "market": market_out,
        "candles_1m_5m": candles,
        "zec_xperp_history": xperp_history,
        "spot_history": spot_history,
        "microstructure": micro_out,
        "read_errors": {
            "feed": feed.get("_read_error"),
            "microstructure": micro.get("_read_error"),
            "history": hist.get("_read_error"),
        },
    }
    output["zec_early_signal"] = evaluate_signal(output, now=now)
    OUT.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUT),
        "bytes": OUT.stat().st_size,
        "schema_version": output["schema_version"],
        "source_status": output["source_status"],
        "market_records": len(market_out),
        "spot_history_ready": {
            market: {bar: item["ready_200"] for bar, item in bars.items()}
            for market, bars in spot_history.items()
        },
        "xperp_history_ready": {bar: item["ready_200"] for bar, item in xperp_history.items()},
        "book_snapshots": output["microstructure"]["book_snapshot_count"],
        "trades": output["microstructure"]["trade_count"],
        "trade_id_gap_events": output["microstructure"]["trade_id_gap_events"],
        "ws_microstructure_status": (output["microstructure"].get("websocket") or {}).get("status"),
        "ws_book_valid": (output["microstructure"].get("websocket") or {}).get("book_valid"),
        "zec_early_signal_status": output["zec_early_signal"]["status"],
        "zec_early_signal_decision": output["zec_early_signal"]["decision"],
        "zec_early_signal_blockers": output["zec_early_signal"]["data_quality"]["blockers"],
    }))


if __name__ == "__main__":
    main()
