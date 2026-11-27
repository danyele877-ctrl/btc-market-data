"""Bounded background retrieval for gaps and recoverable legacy outcomes."""
import json
import logging
import time
from pathlib import Path

from gold_history import INST, request
from gold_market_view import analyze
from gold_paper import ACTIVE, LEGACY_REPLAY, start_at
from gold_tracking import save


def backfill(bar, start, end, step, fetch=request):
    rows = []
    cursor = end * 1000
    for _ in range(12):
        page = fetch("/market/history-candles", instId=INST, bar=bar, limit="300", after=str(cursor))
        if not page:
            break
        oldest = min(int(row[0]) for row in page)
        if oldest >= cursor:
            raise ValueError("OKX history cursor did not move backwards")
        rows.extend(row for row in page if start * 1000 <= int(row[0]) < end * 1000)
        if oldest <= start * 1000:
            break
        cursor = oldest
        time.sleep(0.35)
    return analyze(rows, bar, end, history_limit=None)["bars"] if rows else []


def worker(root):
    root = Path(root)
    path = root / "paper_history.json"
    windows, tried = {}, {}
    while True:
        try:
            ledger = json.loads((root / "paper_ledger.json").read_text())
            now = int(time.time())
            needed = {}
            for key, item in ledger.items():
                legacy = item["state"] in LEGACY_REPLAY and item.get("schema") != 2
                if not legacy and item["state"] not in ACTIVE:
                    continue
                step = item["step"]
                start = start_at(item) if legacy else item["cursor"]
                if not legacy and item["state"] != "DATA_GAP" and now - start < 2 * step + 75:
                    continue
                end = min(now // step * step, item["expires_at"] + item["holding_seconds"])
                if start < end:
                    needed[key] = item["bar"], start, end, step, legacy
            windows = {key: window for key, window in windows.items() if key in needed}
            tried = {key: stamp for key, stamp in tried.items() if key in needed}
            ready = (key for key in needed if now >= tried.get(key, 0))
            for key in sorted(ready, key=lambda k: (needed[k][-1], -needed[k][1]))[:3]:
                bar, start, end, step, legacy = needed[key]
                tried[key] = now + (600 if legacy else 90)
                try:
                    bars = backfill(bar, int(start), int(end), step)
                    windows[key] = {"bars": bars, "through": end}
                except (OSError, TypeError, ValueError):
                    logging.exception("Paper history unavailable at %s (%s)", start, bar)
            if needed or path.exists():
                save(path, {"instrument": INST, "windows": windows})
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception("Paper history cycle failed")
        time.sleep(30)
