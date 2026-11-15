#!/usr/bin/env python3
"""Conservative, public-data queue-depletion proxy for passive ZEC X-Perp limits."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _time_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _round_tick(price: float, tick_size: float) -> float:
    try:
        p, tick = Decimal(str(price)), Decimal(str(tick_size))
        return float((p / tick).quantize(Decimal("1")) * tick)
    except (InvalidOperation, ZeroDivisionError):
        return price


def estimate_queue_depletion(
    history: dict[str, Any],
    entry_time_ms: int,
    limit_price: float,
    side: str,
    tick_size: float,
    fill_window_min: int = 5,
) -> dict[str, Any]:
    """Estimate whether displayed queue would be depleted by aggressive trades.

    This is not a real fill probability: hidden liquidity, cancellations, latency,
    exact exchange queue position and partial fills cannot be reconstructed.
    """
    if side not in ("LONG", "SHORT") or tick_size <= 0 or fill_window_min <= 0:
        return {"status": "DATI NON VALIDI"}
    if history.get("instrument") != INSTRUMENT_ID:
        return {"status": "STRUMENTO NON ESATTO"}
    limit_price = _round_tick(limit_price, tick_size)
    end_ms = entry_time_ms + fill_window_min * 60000
    books = [
        row for row in history.get("book_snapshots", [])
        if isinstance(row, dict) and row.get("source") == "websocket"
        and _num(row.get("ts_ms")) is not None
    ]
    books.sort(key=lambda row: int(row["ts_ms"]))
    before = [row for row in books if 0 <= entry_time_ms - int(row["ts_ms"]) <= 2000]
    if not before:
        return {"status": "BOOK_ENTRY_NON DISPONIBILE", "limit_price": limit_price}
    entry_book = before[-1]
    window_books = [row for row in books if entry_time_ms <= int(row["ts_ms"]) <= end_ms]
    if len(window_books) < max(60, int(fill_window_min * 60 * 0.5)):
        return {"status": "COPERTURA BOOK INSUFFICIENTE", "limit_price": limit_price,
                "book_snapshots": len(window_books)}
    gaps = [
        int(b["ts_ms"]) - int(a["ts_ms"])
        for a, b in zip(window_books, window_books[1:])
    ]
    if not gaps or max(gaps) > 5000:
        return {"status": "GAP BOOK", "limit_price": limit_price,
                "max_book_gap_ms": max(gaps) if gaps else None}

    bids = entry_book.get("bids") or []
    asks = entry_book.get("asks") or []
    best_bid = _num(bids[0][0]) if bids and len(bids[0]) >= 2 else None
    best_ask = _num(asks[0][0]) if asks and len(asks[0]) >= 2 else None
    if best_bid is None or best_ask is None:
        return {"status": "BOOK ENTRY MALFORMATO", "limit_price": limit_price}
    if side == "LONG" and limit_price >= best_ask:
        return {"status": "LIMIT MARKETABILE", "limit_price": limit_price}
    if side == "SHORT" and limit_price <= best_bid:
        return {"status": "LIMIT MARKETABILE", "limit_price": limit_price}

    levels = bids if side == "LONG" else asks
    queue_ahead = 0.0
    level_present = False
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price, size = _num(level[0]), _num(level[1])
        if price is not None and size is not None and abs(price - limit_price) <= tick_size * 0.5:
            queue_ahead += max(0.0, size)
            level_present = True

    capture_rows = []
    for row in history.get("capture_status", []):
        ts = _time_ms(row.get("captured_at"))
        if ts is not None and entry_time_ms <= ts <= end_ms:
            capture_rows.append(row)
    if len(capture_rows) < max(3, fill_window_min - 1):
        return {"status": "COPERTURA TRADES INSUFFICIENTE", "limit_price": limit_price,
                "capture_checks": len(capture_rows)}
    if any(row.get("trade_events_truncated") for row in capture_rows):
        return {"status": "TRADE TAPE TRONCATO", "limit_price": limit_price}
    if any(not row.get("book_valid") for row in capture_rows):
        return {"status": "BOOK NON VALIDO NELLA FINESTRA", "limit_price": limit_price}
    if any((_num(row.get("book_age_seconds")) or 9999) > 5
           or (_num(row.get("trade_age_seconds")) or 9999) > 180 for row in capture_rows):
        return {"status": "MICROSTRUTTURA STALE", "limit_price": limit_price}
    trade_gap_counts = [_num(row.get("trade_gap_count")) for row in capture_rows]
    book_gap_counts = [_num(row.get("book_gap_count")) for row in capture_rows]
    if any(value is None for value in trade_gap_counts + book_gap_counts) or any(
        later > earlier for earlier, later in zip(trade_gap_counts, trade_gap_counts[1:])
    ) or any(later > earlier for earlier, later in zip(book_gap_counts, book_gap_counts[1:])):
        return {"status": "GAP SEQUENZA NELLA FINESTRA", "limit_price": limit_price}

    trades = []
    for row in history.get("trades", []):
        if not isinstance(row, dict) or row.get("source") != "websocket":
            continue
        ts, price, size = _num(row.get("ts_ms")), _num(row.get("px")), _num(row.get("sz"))
        if ts is None or price is None or size is None or not entry_time_ms < ts <= end_ms:
            continue
        trades.append((int(ts), row, price, size))
    trades.sort(key=lambda item: item[0])
    if any(row.get("gap_before") for _, row, _, _ in trades):
        return {"status": "GAP TRADE NELLA FINESTRA", "limit_price": limit_price}

    if side == "LONG":
        aggressive = [size for _, _, price, size in trades if _["side"] == "sell" and price <= limit_price + tick_size*0.5]
    else:
        aggressive = [size for _, _, price, size in trades if _["side"] == "buy" and price >= limit_price - tick_size*0.5]
    aggressive_volume = sum(aggressive)
    depleted = aggressive_volume > queue_ahead and aggressive_volume > 0
    return {
        "status": "STIMA QUEUE DISPONIBILE",
        "limit_price": limit_price,
        "side": side,
        "queue_ahead_contracts": round(queue_ahead, 8),
        "queue_level_present": level_present,
        "queue_ahead_assumption": "visible queue at exact level; if absent, assumes new level with zero displayed queue; hidden liquidity unknown",
        "aggressive_volume_at_or_better_contracts": round(aggressive_volume, 8),
        "queue_depleted_proxy": bool(depleted),
        "book_snapshots": len(window_books),
        "max_book_gap_ms": max(gaps),
        "capture_checks": len(capture_rows),
        "trade_events": len(trades),
        "fill_window_min": fill_window_min,
        "caveat": "queue-depletion proxy, not actual execution or calibrated fill probability; cancellations, hidden liquidity, latency and partial fills are unknown",
    }
