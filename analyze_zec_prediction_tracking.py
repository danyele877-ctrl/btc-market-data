#!/usr/bin/env python3
"""Analyze BASELINE/SHADOW prediction outcomes without treating overlapping bars as independent."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

DEFAULT_ARCHIVE = Path("data/zec_prediction_tracking.json")
HORIZONS = (5, 15, 30, 40)
DIRECTIONS = ("LONG", "SHORT")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _regime_label(prediction: dict[str, Any]) -> str:
    regime = ((prediction.get("shadow") or {}).get("regime") or {})
    if isinstance(regime, dict):
        combined = regime.get("status") or regime.get("label")
        if combined:
            return str(combined)
        one_h = (regime.get("1H") or {}).get("status")
        four_h = (regime.get("4H") or {}).get("status")
        if one_h or four_h:
            return f"1H={one_h or 'N/D'} | 4H={four_h or 'N/D'}"
    return "N/D"


def _non_overlapping(rows: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
    """Keep at most one forecast per non-overlapping forward window, per engine."""
    ordered = sorted(rows, key=lambda row: int(row.get("entry_close_ms") or 0))
    selected = []
    next_allowed = None
    window_ms = horizon * 60_000
    for row in ordered:
        start = _number(row.get("entry_close_ms"))
        if start is None:
            continue
        start = int(start)
        if next_allowed is None or start >= next_allowed:
            selected.append(row)
            next_allowed = start + window_ms
    return selected


def _stats(rows: list[dict[str, Any]], engine: str, horizon: int) -> dict[str, Any]:
    matured = []
    for row in rows:
        outcome = ((row.get("outcomes") or {}).get(engine) or {}).get(str(horizon)) or {}
        if outcome.get("status") == "MATURO":
            matured.append((row, outcome))

    counts = {key: 0 for key in ("TP1", "SL", "AMBIGUO", "NO_BARRIER")}
    returns, mfe, mae = [], [], []
    for _row, outcome in matured:
        barrier = outcome.get("barrier_outcome")
        if barrier in counts:
            counts[barrier] += 1
        value = _number(outcome.get("directional_return_pct"))
        if value is not None:
            returns.append(value)
        value = _number(outcome.get("mfe_atr"))
        if value is not None:
            mfe.append(value)
        value = _number(outcome.get("mae_atr"))
        if value is not None:
            mae.append(value)

    decisive = counts["TP1"] + counts["SL"]
    return {
        "matured": len(matured),
        "tp1": counts["TP1"],
        "sl": counts["SL"],
        "ambiguous": counts["AMBIGUO"],
        "no_barrier": counts["NO_BARRIER"],
        "tp1_before_sl_rate_excluding_ambiguous": round(counts["TP1"] / decisive, 4) if decisive else None,
        "mean_directional_return_pct": round(statistics.mean(returns), 5) if returns else None,
        "median_mfe_atr": round(statistics.median(mfe), 4) if mfe else None,
        "median_mae_atr": round(statistics.median(mae), 4) if mae else None,
        "sample_note": "proxy su close di riferimento, non fill reale, PnL o probabilità calibrata",
    }


def analyze_archive(archive: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(archive, dict) or archive.get("schema_version") != 1:
        raise ValueError("tracking archive schema_version must be 1")
    if archive.get("instrument") != "ZEC-USD_UM_XPERP-310530":
        raise ValueError("tracking archive instrument mismatch")
    predictions = archive.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError("tracking archive predictions must be a list")

    result: dict[str, Any] = {
        "schema_version": 1,
        "instrument": archive["instrument"],
        "updated_at": archive.get("updated_at"),
        "predictions_total": len(predictions),
        "engines": {},
        "limitations": [
            "le righe per minuto sono osservazioni sovrapposte, non trade indipendenti",
            "la selezione non-overlapping riduce la dipendenza temporale ma non dimostra indipendenza statistica",
            "gli outcome usano il close di riferimento, non fill LIMIT reali",
            "commissioni, slippage, funding e latenza non sono inclusi",
            "le frequenze non sono probabilità calibrate e non dimostrano un edge predittivo",
        ],
    }

    for engine in ("baseline", "shadow"):
        engine_rows = [
            row for row in predictions
            if isinstance(row, dict)
            and (row.get(engine) or {}).get("direction") in DIRECTIONS
        ]
        confirmed_rows = [
            row for row in engine_rows
            if (row.get(engine) or {}).get("decision") == (row.get(engine) or {}).get("direction")
        ]
        engine_result: dict[str, Any] = {
            "predictions_with_direction": len(engine_rows),
            "confirmed_directional_triggers": len(confirmed_rows),
            "by_direction": {},
            "confirmed_triggers_by_direction": {},
        }
        for direction in DIRECTIONS:
            direction_rows = [row for row in engine_rows if (row.get(engine) or {}).get("direction") == direction]
            confirmed_direction_rows = [
                row for row in confirmed_rows if (row.get(engine) or {}).get("direction") == direction
            ]
            engine_result["by_direction"][direction] = {}
            engine_result["confirmed_triggers_by_direction"][direction] = {}
            for horizon in HORIZONS:
                raw = _stats(direction_rows, engine, horizon)
                selected_all = _non_overlapping(engine_rows, horizon)
                selected_direction = [
                    row for row in selected_all if (row.get(engine) or {}).get("direction") == direction
                ]
                raw_confirmed = _stats(confirmed_direction_rows, engine, horizon)
                selected_confirmed_all = _non_overlapping(confirmed_rows, horizon)
                selected_confirmed = [
                    row for row in selected_confirmed_all if (row.get(engine) or {}).get("direction") == direction
                ]
                engine_result["by_direction"][direction][str(horizon)] = {
                    "all_observations": raw,
                    "non_overlapping": _stats(selected_direction, engine, horizon),
                }
                engine_result["confirmed_triggers_by_direction"][direction][str(horizon)] = {
                    "all_observations": raw_confirmed,
                    "non_overlapping": _stats(selected_confirmed, engine, horizon),
                }

        regime_groups: dict[str, list[dict[str, Any]]] = {}
        for row in engine_rows:
            regime_groups.setdefault(_regime_label(row), []).append(row)
        engine_result["shadow_regime_breakdown"] = {}
        if engine == "shadow":
            for regime, rows in sorted(regime_groups.items()):
                engine_result["shadow_regime_breakdown"][regime] = {
                    str(h): _stats(_non_overlapping(rows, h), engine, h)
                    for h in HORIZONS
                }
        result["engines"][engine] = engine_result
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    report = analyze_archive(archive)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
