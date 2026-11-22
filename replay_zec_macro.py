#!/usr/bin/env python3
"""Replay scheduled events on exact OKX Europe 1m candles."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlencode

from candle_service import parse_candle
from capture_render_feed import OKX_EEA
from zec_limit_tracking import simulate
from zec_macro_shadow import BTC_ID, EVENTS, PRE_MINUTES, WATCH_MINUTES, ZEC_ID, event_context, minute_bars


def history(instrument, through):
    params = urlencode({"instId": instrument, "bar": "1m", "limit": 100, "after": through * 1000})
    response = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "15",
         f"{OKX_EEA}/market/history-candles?{params}"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(response.stdout)
    if payload.get("code") != "0":
        raise ValueError(f"OKX Europe: {instrument}, code {payload.get('code')}")
    return [bar for row in payload["data"] if (bar := parse_candle(row, "candle1m")) is not None]


def archived_history(event, snapshots, through):
    start = event.at - PRE_MINUTES * 60
    latest = {}
    for commit, snapshot in snapshots:
        if snapshot.get("feed_source_mode") not in ("OKX_EEA_REST_WS_SNAPSHOT", "OKX_EEA_REST_SNAPSHOT"):
            continue
        history = snapshot.get("predictive_history") or {}
        btc, zec = history.get("btc_1m", []), history.get("xperp_1m", [])
        overlap = len(minute_bars(btc, start, through).keys() & minute_bars(zec, start, through).keys())
        if overlap > latest.get("overlap", 0):
            latest = {"commit": commit, "btc": btc, "zec": zec, "overlap": overlap}
    return latest


def run_event(event, fee_bps, slippage_bps, snapshots=()):
    end = event.at + (WATCH_MINUTES - 1) * 60
    through = end + 46 * 60
    archived = archived_history(event, snapshots, through)
    if archived:
        btc, zec = archived["btc"], archived["zec"]
        market_data = {"source": "radar_snapshot", "commit": archived["commit"]}
    else:
        cursor = (through + 3599) // 3600 * 3600
        cursors = [cursor - 3600, cursor] if cursor - 100 * 60 > event.at - PRE_MINUTES * 60 else [cursor]
        btc, zec = ([bar for point in cursors for bar in history(instrument, point)]
                    for instrument in (BTC_ID, ZEC_ID))
        market_data = {"source": "OKX_EEA_REST"}

    report = event_context(event, btc, zec, datetime.fromtimestamp(end, timezone.utc))
    report["market_data"] = market_data
    if report["status"] != "retest_observed":
        return report

    entry, stop = report["reference_limit"], report["reference_stop"]
    side = report["side"]
    sign = 1 if side == "LONG" else -1
    risk = abs(entry - stop)
    # Observe the retest at close and allow for collection delay. The shared
    # simulator excludes bars overlapping the resulting observation time.
    signal_at = report["retest_at"] + 15
    plan = {"signal_at": signal_at, "fill_by": signal_at + 300, "outcome_by": signal_at + 2700,
            "side": side, "entry": entry, "stop": stop, "tp1": entry + sign * risk,
            "invalidation": stop, "reference_risk": risk}
    bars = minute_bars(zec, signal_at, through)
    outcome = simulate(plan, bars, strict=True)
    outcome.pop("net_reference_r", None)
    outcome.pop("net_equal_risk_r", None)
    if outcome.get("filled") and "gross_r" in outcome:
        exit_price = entry + sign * risk * outcome["gross_r"]
        # Slippage is a sensitivity assumption on the exit. A strict fill
        # proxy can still differ from actual queue position.
        costs = (fee_bps * (entry + exit_price) + slippage_bps * exit_price) / 10_000
        outcome["net_r_estimate"] = round(outcome["gross_r"] - costs / risk, 4)
    report.update(plan=plan, outcome=outcome)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", nargs="+", choices=[event.id for event in EVENTS])
    parser.add_argument("--fee-bps", type=float, default=5, help="Illustrative fee on each leg")
    parser.add_argument("--slippage-bps", type=float, default=2, help="Illustrative slippage on exit")
    parser.add_argument("--snapshot", action="append", default=[], metavar="COMMIT",
                        help="Read an archived data/radar_snapshot.json from Git")
    args = parser.parse_args()
    if not (0 <= args.fee_bps < 10_000 and 0 <= args.slippage_bps < 10_000):
        parser.error("fee and slippage must be between 0 and 10,000 bps")
    snapshots = [
        (commit, json.loads(subprocess.check_output(
            ["git", "show", f"{commit}:data/radar_snapshot.json"], text=True)))
        for commit in args.snapshot
    ]
    reports = []
    for event in EVENTS:
        if event.id in args.events:
            try:
                reports.append(run_event(event, args.fee_bps, args.slippage_bps, snapshots))
            except (ValueError, subprocess.CalledProcessError) as exc:
                reports.append({"event": event._asdict(), "status": "history_unavailable", "reason": str(exc)})
    print(json.dumps({"fee_bps": args.fee_bps, "exit_slippage_bps": args.slippage_bps,
                      "events": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
