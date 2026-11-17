#!/usr/bin/env python3
"""Paired, non-overlapping LONG/SHORT diagnostics for the ZEC predictive archive.

These are label diagnostics, not strategy backtests, real fills, PnL, or calibrated
probabilities. Events are paired by timestamp and sampled at least one horizon
apart to reduce overlap between forward outcome windows.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FAMILIES = ("scalp_5m", "scalp_micro_5m")
HORIZONS = (5, 15, 30, 40)


def _complete_label(event: dict[str, Any]) -> bool:
    label = event.get("label")
    if not isinstance(label, dict) or not isinstance(label.get("filled_proxy"), bool):
        return False
    return label["filled_proxy"] is False or isinstance(label.get("tp_before_sl"), bool)


def _paired_events(events: list[dict[str, Any]], family: str, horizon: int) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    for event in events:
        if event.get("family") != family or event.get("horizon_min") != horizon:
            continue
        side = event.get("side")
        timestamp = event.get("timestamp_ms")
        if side not in ("LONG", "SHORT") or not isinstance(timestamp, int):
            continue
        grouped.setdefault(timestamp, {})[side] = event
    pairs = []
    for timestamp, sides in sorted(grouped.items()):
        long_event, short_event = sides.get("LONG"), sides.get("SHORT")
        if long_event and short_event and _complete_label(long_event) and _complete_label(short_event):
            pairs.append({"timestamp_ms": timestamp, "LONG": long_event, "SHORT": short_event})
    return pairs


def _non_overlapping(pairs: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
    selected = []
    next_available = -1
    spacing = horizon * 60_000
    for pair in sorted(pairs, key=lambda item: item["timestamp_ms"]):
        timestamp = pair["timestamp_ms"]
        if timestamp >= next_available:
            selected.append(pair)
            next_available = timestamp + spacing
    return selected


def _side_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    filled = [event for event in events if event["label"]["filled_proxy"] is True]
    wins = sum(event["label"].get("tp_before_sl") is True for event in filled)
    losses = sum(event["label"].get("tp_before_sl") is False for event in filled)
    resolved = wins + losses
    return {
        "observations": total,
        "filled_proxy": len(filled),
        "not_filled_proxy": total - len(filled),
        "fill_rate_proxy": round(len(filled) / total, 4) if total else None,
        "resolved_fills": resolved,
        "tp_before_sl": wins,
        "sl_or_no_tp_before_horizon": losses,
        "tp_before_sl_rate_conditional_on_fill": round(wins / resolved, 4) if resolved else None,
    }


def analyze(archive: dict[str, Any]) -> dict[str, Any]:
    events = archive.get("events") if isinstance(archive.get("events"), list) else []
    report: dict[str, Any] = {
        "instrument": archive.get("instrument"),
        "archive_updated_at": archive.get("updated_at"),
        "archive_events_total": len(events),
        "method": "paired LONG/SHORT labels; non-overlapping event starts spaced by at least the full outcome horizon",
        "families": {},
        "limitations": [
            "filled_proxy is a candle-touch proxy, not an exchange execution or queue-confirmed fill",
            "TP/SL labels use OHLC candles; if both barriers touch in one candle, the label conservatively assumes SL first",
            "non-overlap reduces shared forward windows but does not make market observations statistically independent",
            "short archive and limited market regimes do not validate a predictive edge or calibrated probability",
            "fees, slippage, latency, funding and actual execution are not included",
        ],
    }
    for family in FAMILIES:
        family_report = {}
        for horizon in HORIZONS:
            pairs = _paired_events(events, family, horizon)
            selected = _non_overlapping(pairs, horizon)
            long_events = [pair["LONG"] for pair in selected]
            short_events = [pair["SHORT"] for pair in selected]
            first = selected[0]["timestamp_ms"] if selected else None
            last = selected[-1]["timestamp_ms"] if selected else None
            family_report[str(horizon)] = {
                "paired_complete_events": len(pairs),
                "non_overlapping_paired_events": len(selected),
                "first_event_utc": datetime.fromtimestamp(first / 1000, timezone.utc).isoformat() if first else None,
                "last_event_utc": datetime.fromtimestamp(last / 1000, timezone.utc).isoformat() if last else None,
                "LONG": _side_metrics(long_events),
                "SHORT": _side_metrics(short_events),
            }
        report["families"][family] = family_report
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/zec_predictive_archive.json"))
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    print(json.dumps(analyze(archive), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
