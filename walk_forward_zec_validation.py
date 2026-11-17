#!/usr/bin/env python3
"""Expanding-window walk-forward diagnostics for the ZEC analog model.

This is an offline label audit, not an execution backtest, PnL estimate, or
calibrated probability. Every forecast is evaluated using only labels whose
forward window had fully matured before that forecast's origin.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from zec_predictive_engine import FEATURES, _analogs

LEGACY_FEATURES = tuple(name for name in FEATURES if name != 'btc_trend_20')

DEFAULT_ARCHIVE = Path("data/zec_predictive_archive.json")
FAMILIES = ("scalp_5m",)
HORIZONS = (5, 15, 30, 40)
SIDES = ("LONG", "SHORT")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _origin_ms(event: dict[str, Any]) -> int | None:
    stamp = _number(event.get("timestamp_ms"))
    if stamp is None:
        return None
    # scalp_5m timestamps are candle-open times; model features are available
    # only after that 5m candle has closed.
    return int(stamp + 5 * 60_000)


def _matured_binary(event: dict[str, Any]) -> bool:
    label = event.get("label")
    return (
        isinstance(label, dict)
        and label.get("filled_proxy") is True
        and isinstance(label.get("tp_before_sl"), bool)
    )


def _stratum(value: Any, prefix: str) -> str:
    number = _number(value)
    if number is None:
        return "N/D"
    if number > 0:
        return prefix + "_UP"
    if number < 0:
        return prefix + "_DOWN"
    return prefix + "_FLAT"


def _setup_proxy(features: dict[str, Any]) -> str:
    r1 = _number(features.get('ret1'))
    r3 = _number(features.get('ret3'))
    atr = _number(features.get('atr_pct'))
    rsi = _number(features.get('rsi14'))
    trend = _number(features.get('trend'))
    if any(v is None for v in (r1, r3, atr, rsi, trend)) or atr <= 0:
        return 'N/D'
    if r1 >= 0.5 * atr and r3 > 0:
        return 'BREAKOUT_UP_PROXY'
    if r1 <= -0.5 * atr and r3 < 0:
        return 'BREAKOUT_DOWN_PROXY'
    if rsi <= 0.35 and r1 > 0:
        return 'REVERSAL_UP_PROXY'
    if rsi >= 0.65 and r1 < 0:
        return 'REVERSAL_DOWN_PROXY'
    if abs(trend) >= 0.003 and trend * r3 > 0:
        return 'TREND_CONTINUATION_PROXY'
    return 'RANGE_OR_MIXED_PROXY'

def _non_overlapping(rows: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
    selected = []
    next_allowed = None
    for row in sorted(rows, key=lambda item: item['timestamp_ms']):
        start = row['timestamp_ms']
        if next_allowed is None or start >= next_allowed:
            selected.append(row)
            next_allowed = start + horizon * 60_000
    return selected

def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"scored": 0, "brier_score": None, "baseline_brier_score": None, "brier_skill_score": None,
                "mean_probability": None, "observed_rate": None, "directional_accuracy_at_50": None,
                "balanced_accuracy_at_50": None, "precision_at_50": None, "sensitivity_at_50": None,
                "specificity_at_50": None, "predicted_positive_at_50": 0}
    probabilities = [row["probability"] for row in rows]
    outcomes = [int(row["outcome"]) for row in rows]
    count = len(rows)
    observed = sum(outcomes) / count
    brier = sum((p-y)**2 for p,y in zip(probabilities,outcomes)) / count
    baseline_brier = sum((observed-y)**2 for y in outcomes) / count
    tp = sum(p >= 0.5 and y == 1 for p,y in zip(probabilities,outcomes))
    fp = sum(p >= 0.5 and y == 0 for p,y in zip(probabilities,outcomes))
    fn = sum(p < 0.5 and y == 1 for p,y in zip(probabilities,outcomes))
    tn = sum(p < 0.5 and y == 0 for p,y in zip(probabilities,outcomes))
    sensitivity = tp/(tp+fn) if tp+fn else None
    specificity = tn/(tn+fp) if tn+fp else None
    precision = tp/(tp+fp) if tp+fp else None
    balanced = (sensitivity+specificity)/2 if sensitivity is not None and specificity is not None else None
    return {
        "scored": count, "brier_score": round(brier,6),
        "baseline_brier_score": round(baseline_brier,6),
        "brier_skill_score": round(1-brier/baseline_brier,4) if baseline_brier>0 else None,
        "mean_probability": round(sum(probabilities)/count,4), "observed_rate": round(observed,4),
        "directional_accuracy_at_50": round((tp+tn)/count,4),
        "balanced_accuracy_at_50": round(balanced,4) if balanced is not None else None,
        "precision_at_50": round(precision,4) if precision is not None else None,
        "sensitivity_at_50": round(sensitivity,4) if sensitivity is not None else None,
        "specificity_at_50": round(specificity,4) if specificity is not None else None,
        "predicted_positive_at_50": tp+fp,
    }


def _evaluate_group(events: list[dict[str, Any]], family: str, side: str,
                    horizon: int, test_stamps: set[int]) -> tuple[list[dict[str, Any]], int]:
    scored: list[dict[str, Any]] = []
    eligible_test = 0
    ordered = sorted(
        (event for event in events
         if event.get("family") == family
         and event.get("side") == side
         and event.get("horizon_min") == horizon
         and isinstance(event.get("features"), dict)
         and _origin_ms(event) in test_stamps
         and _matured_binary(event)),
        key=lambda item: int(item["timestamp_ms"]),
    )
    for event in ordered:
        eligible_test += 1
        origin_ms = _origin_ms(event)
        estimate = _analogs(
            {"events": events}, family, side, horizon,
            event["features"], feature_names=FEATURES, as_of_ms=origin_ms,
        )
        probability = _number(estimate.get("p_tp_before_sl_given_touch"))
        feature_mode = "FULL_BTC_CONTEXT"
        if probability is None and all(_number(event["features"].get(key)) is not None for key in LEGACY_FEATURES):
            legacy = _analogs(
                {"events": events}, family, side, horizon,
                event["features"], feature_names=LEGACY_FEATURES, as_of_ms=origin_ms,
            )
            probability = _number(legacy.get("p_tp_before_sl_given_touch"))
            if probability is not None:
                feature_mode = "LEGACY_NO_BTC_CONTEXT"
        if probability is None:
            continue
        scored.append({
            "probability": max(0.0, min(1.0, probability)),
            "outcome": 1 if event["label"]["tp_before_sl"] else 0,
            "btc_regime": _stratum(event["features"].get("btc_trend_20"), "BTC"),
            "zec_trend": _stratum(event["features"].get("trend"), "ZEC"),
            "feature_mode": feature_mode,
            "timestamp_ms": int(event["timestamp_ms"]),
            "setup_proxy": _setup_proxy(event["features"]),
            "btc_context": "PRESENT" if _number(event["features"].get("btc_trend_20")) is not None else "LEGACY_MISSING",
        })
    return scored, eligible_test


def analyze(archive: dict[str, Any], folds: int = 4, warmup_fraction: float = 0.5) -> dict[str, Any]:
    events = archive.get("events") if isinstance(archive.get("events"), list) else []
    stamps = sorted({
        origin for event in events
        if event.get("family") in FAMILIES
        and event.get("horizon_min") in HORIZONS
        and (origin := _origin_ms(event)) is not None
    })
    if folds < 1 or not 0.1 <= warmup_fraction < 0.9:
        raise ValueError("folds deve essere >=1 e warmup_fraction tra 0.1 e 0.9")
    warmup_count = int(len(stamps) * warmup_fraction)
    test_stamps = stamps[warmup_count:]
    if warmup_count < 1 or len(test_stamps) < folds:
        raise ValueError("storico insufficiente per creare finestre walk-forward")
    chunks = [chunk for chunk in (
        test_stamps[index * len(test_stamps) // folds:(index + 1) * len(test_stamps) // folds]
        for index in range(folds)
    ) if chunk]

    fold_reports = []
    overall: dict[str, list[dict[str, Any]]] = defaultdict(list)
    overall_eligible: dict[str, int] = defaultdict(int)
    strata: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for fold_index, chunk in enumerate(chunks, start=1):
        chunk_set = set(chunk)
        fold_scored = 0
        fold_eligible = 0
        for family in FAMILIES:
            for horizon in HORIZONS:
                for side in SIDES:
                    key = f"{family}|{side}|{horizon}m"
                    scored, eligible = _evaluate_group(events, family, side, horizon, chunk_set)
                    overall[key].extend(scored)
                    overall_eligible[key] += eligible
                    fold_scored += len(scored)
                    fold_eligible += eligible
                    for row in scored:
                        strata[key][row["btc_regime"]].append(row)
                        strata[key][row["zec_trend"]].append(row)
        fold_reports.append({
            "fold": fold_index,
            "test_start_ms": chunk[0],
            "test_end_ms": chunk[-1],
            "test_timestamps": len(chunk),
            "eligible_test_outcomes": fold_eligible,
            "scored_forecasts": fold_scored,
        })

    by_group = {}
    for key, rows in overall.items():
        horizon = int(key.rsplit('|', 1)[1].replace('m', ''))
        non_overlap = _non_overlapping(rows, horizon)
        setup_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        btc_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        feature_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in non_overlap:
            setup_groups[row['setup_proxy']].append(row)
            btc_groups[row['btc_context']].append(row)
            feature_groups[row['feature_mode']].append(row)
        by_group[key] = {
            **_metrics(rows),
            'all_test_observations': _metrics(rows),
            'non_overlapping_primary': _metrics(non_overlap),
            'eligible_test_outcomes': overall_eligible[key],
            'coverage': round(len(rows) / overall_eligible[key], 4) if overall_eligible[key] else None,
            'non_overlapping_coverage': round(len(non_overlap) / overall_eligible[key], 4) if overall_eligible[key] else None,
            'feature_modes_all': {mode: sum(1 for row in rows if row.get('feature_mode') == mode) for mode in ('FULL_BTC_CONTEXT', 'LEGACY_NO_BTC_CONTEXT')},
            'feature_modes': {mode: sum(1 for row in rows if row.get('feature_mode') == mode) for mode in ('FULL_BTC_CONTEXT', 'LEGACY_NO_BTC_CONTEXT')},
            'non_overlapping_by_feature_mode': {name: _metrics(items) for name, items in sorted(feature_groups.items())},
            'non_overlapping_by_btc_context': {name: _metrics(items) for name, items in sorted(btc_groups.items())},
            'non_overlapping_by_setup_proxy': {name: _metrics(items) for name, items in sorted(setup_groups.items())},
            'by_btc_regime_all_observations': {name: _metrics(items) for name, items in sorted(strata[key].items()) if name.startswith('BTC_')},
            'by_btc_regime': {name: _metrics(items) for name, items in sorted(strata[key].items()) if name.startswith('BTC_')},
            'by_zec_trend_all_observations': {name: _metrics(items) for name, items in sorted(strata[key].items()) if name.startswith('ZEC_')},
        }

    scored_total = sum(len(rows) for rows in overall.values())
    return {
        "validation_status": "DESCRIPTIVE_ONLY" if scored_total else "INSUFFICIENT_SCORABLE_FORECASTS",
        "scored_total": scored_total,
        "instrument": archive.get("instrument"),
        "archive_updated_at": archive.get("updated_at"),
        "archive_events_total": len(events),
        "method": "expanding chronological walk-forward; uses only earlier matured labels; reports raw and non-overlapping test metrics, BTC-context availability, and explicitly heuristic setup proxies",
        "warmup_fraction": warmup_fraction,
        "folds": fold_reports,
        "results": by_group,
        "limitations": [
            "scalp_5m only; swing and microstructure families are not included",
            "LONG and SHORT are scored separately; BTC and ZEC trend strata use the sign of the stored feature",
            "breakout/reversal/continuation groups are feature-only heuristic proxies, not manually verified chart patterns; thresholds are descriptive and not performance-tuned",
            "primary metrics use greedy non-overlapping test windows; raw metrics remain available for coverage comparison",
            "BTC-context-present and legacy-missing groups are reported separately; legacy rows are not BTC-conditioned",
            "the score is conditional on filled_proxy and uses a candle-touch proxy, not confirmed exchange fills",
            "Brier scores on a small, regime-limited archive are descriptive and do not establish calibrated probabilities or predictive edge",
            "legacy fallback scores pre-BTC-context rows using nine features and labels them separately; those rows are not BTC-context-conditioned",
            "zero scored forecasts means validation cannot assess predictive accuracy; do not treat a successful CI run as model validation",
            "fees, slippage, latency, funding, and execution are not modeled",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--warmup-fraction", type=float, default=0.5)
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    print(json.dumps(analyze(archive, args.folds, args.warmup_fraction), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
