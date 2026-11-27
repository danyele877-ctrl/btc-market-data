"""Independent quote freshness checks and rate-limited OKX Europe REST fallback."""
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gold_history import INST, request
from gold_tracking import save


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def fresh(row, now, age):
    try:
        return 0 <= now - int(row["ts"]) / 1000 <= age
    except (KeyError, TypeError, ValueError):
        return False


def valid_book(row, now, age):
    if not fresh(row, now, age):
        return False
    try:
        bid, ask = float(row["bids"][0][0]), float(row["asks"][0][0])
        return 0 < bid <= ask and math.isfinite(ask)
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def valid_trade(row, now, age):
    if not fresh(row, now, age) or row.get("instId") != INST:
        return False
    try:
        price, size = float(row["px"]), float(row["sz"])
        return 0 < price < math.inf and 0 < size < math.inf
    except (KeyError, TypeError, ValueError):
        return False


def latest_trade(rows, now):
    return max((r for r in rows if valid_trade(r, now, 180)),
               key=lambda r: (int(r["ts"]), str(r.get("tradeId","")).zfill(32)), default=None)


def snapshot(root, now):
    ws = load(Path(root) / "live_quote.json")
    rest = load(Path(root) / "rest_quote.json")
    ws_ok = ws.get("connected") and 0 <= now - ws.get("observed_at", 0) <= 5
    book, book_source = None, None
    if ws_ok and valid_book(ws.get("book"), now, 5):
        book, book_source = ws["book"], "OKX_EEA_WS"
    elif valid_book(rest.get("book"), now, 8):
        book, book_source = rest["book"], "OKX_EEA_REST"
    trades = [(x["last_trade"], source) for x, source in ((ws, "OKX_EEA_WS"), (rest, "OKX_EEA_REST"))
              if valid_trade(x.get("last_trade"), now, 180)]
    trade, trade_source = max(trades, key=lambda pair: int(pair[0]["ts"]), default=(None, None))
    forming = ws.get("forming_1m") if ws_ok else None
    if forming and not forming[0] <= now < forming[0] + 60:
        forming = None
    return dict(connected=bool(ws_ok), observed_at=ws.get("observed_at", 0), book=book,
                book_source=book_source, last_trade=trade, trade_source=trade_source,
                flow=ws.get("flow", {}) if ws_ok else {}, forming_1m=forming, server_ts=now)


def rest_worker(root):
    root = Path(root)
    due = {}
    values = {}
    endpoints = {"book": ("/market/books", "1"), "last_trade": ("/market/trades", "10")}
    with ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            now = time.time()
            data = snapshot(root, now)
            pending = {}
            for key, source in (("book", "book_source"), ("last_trade", "trade_source")):
                if data[source] == "OKX_EEA_WS" and (key == "book" or fresh(data[key], now, 30)):
                    continue
                if now < due.get(key, 0):
                    continue
                endpoint, limit = endpoints[key]
                pending[key] = pool.submit(request, endpoint, instId=INST, limit=limit)
            for key, future in pending.items():
                try:
                    rows = future.result()
                    now = time.time()
                    candidate = (rows[0] if rows and valid_book(rows[0], now, 8) else None) if key=="book" else latest_trade(rows, now)
                    if candidate and (not values.get(key) or int(candidate["ts"]) >= int(values[key]["ts"])):
                        values[key] = candidate
                    due[key] = now + (3 if key == "book" else 12)
                except Exception:
                    due[key] = time.time() + 30
                    logging.exception("Gold REST quote unavailable: %s", key)
            if pending:
                try:
                    save(root / "rest_quote.json", values)
                except OSError:
                    logging.exception("Could not publish REST quote")
            time.sleep(2)
