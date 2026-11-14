#!/usr/bin/env python3
"""Replay versioned radar snapshots without orders, sizing, or PnL calculations."""
import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone

from zec_early_signal import INSTRUMENT_ID, evaluate_signal


PATH = "data/radar_snapshot.json"


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def latest_candle(snapshot):
    series = (((snapshot.get("candles_1m_5m") or {}).get("ZEC-USD_UM_XPERP") or {}).get("5m") or {})
    return series.get("latest") or ((series.get("tail") or [None])[-1])


def history_snapshots(limit=None):
    commits = git("log", "--format=%H", "--reverse", "--", PATH).splitlines()
    if limit:
        commits = commits[-limit:]
    output = []
    for commit in commits:
        try:
            raw = git("show", f"{commit}:{PATH}")
            value = json.loads(raw)
            captured = parse_time(value.get("source_captured_at"))
            if captured:
                output.append((captured, value))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
    return sorted(output, key=lambda item: item[0])


def replay(rows):
    # Reconstruct the OI context from prior snapshots only; each evaluation
    # sees no future OI sample. Exact instrument IDs are enforced.
    oi_points = []
    evaluated = []
    for captured, snapshot in rows:
        market = snapshot.get("market", {})
        oi_record = market.get("zec_xperp_open_interest", {})
        ticker = market.get("zec_xperp_ticker", {})
        oi = ((oi_record.get("first") or {}).get("oi"))
        price = ((ticker.get("first") or {}).get("last"))
        inst_id = ((ticker.get("first") or {}).get("instId"))
        ts = ((oi_record.get("first") or {}).get("ts")) or ((ticker.get("first") or {}).get("ts"))
        if inst_id == INSTRUMENT_ID and oi and price and ts:
            oi_points.append({"ts_ms": int(ts), "oi_contracts": str(oi), "price_usd": str(price), "inst_id": INSTRUMENT_ID})
        micro = snapshot.setdefault("microstructure", {})
        micro["open_interest_history"] = [
            point for point in oi_points
            if captured.timestamp() * 1000 - point["ts_ms"] <= 20 * 60 * 1000
        ]
        signal = evaluate_signal(snapshot, now=captured)
        candle = latest_candle(snapshot)
        evaluated.append({"captured": captured, "signal": signal, "candle": candle})

    counts = Counter(item["signal"]["status"] for item in evaluated)
    decisions = Counter(item["signal"]["decision"] for item in evaluated)
    outcomes = {"5m": Counter(), "15m": Counter()}
    trigger_count = 0
    for index, item in enumerate(evaluated):
        signal = item["signal"]
        if signal["status"] != "TRIGGER CONFERMATO" or not item["candle"]:
            continue
        trigger_count += 1
        base = float(item["candle"]["close"])
        close_time = parse_time(item["candle"].get("completed_at")) or item["captured"]
        for horizon, delta in (("5m", timedelta(minutes=5)), ("15m", timedelta(minutes=15))):
            target_time = close_time + delta
            future = next((candidate for candidate in evaluated[index + 1:]
                           if candidate["candle"] and
                           (parse_time(candidate["candle"].get("completed_at")) or candidate["captured"]) >= target_time), None)
            if not future:
                outcomes[horizon]["non valutabile"] += 1
                continue
            future_close = float(future["candle"]["close"])
            direction = signal["decision"]
            favorable = future_close > base if direction == "LONG" else future_close < base
            neutral = future_close == base
            outcomes[horizon]["favorevole" if favorable else "neutrale" if neutral else "contrario"] += 1

    return {
        "snapshots": len(evaluated),
        "first_capture": rows[0][0].isoformat() if rows else None,
        "last_capture": rows[-1][0].isoformat() if rows else None,
        "status_counts": dict(counts),
        "decision_counts": dict(decisions),
        "confirmed_triggers": trigger_count,
        "directional_candle_outcomes_no_pnl": {key: dict(value) for key, value in outcomes.items()},
        "caveat": "Replay descrittivo di snapshot salvati, non backtest di esecuzione: campioni radi e sovrapposti, regime singolo, nessun PnL né size.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="limita agli ultimi N snapshot")
    args = parser.parse_args()
    print(json.dumps(replay(history_snapshots(args.limit)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
