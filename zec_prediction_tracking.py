#!/usr/bin/env python3
"""Prediction tracking for BASELINE and SHADOW; diagnostic only, no orders or sizing."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
ARCHIVE_PATH = Path("data/zec_prediction_tracking.json")
HORIZONS = (5, 15, 30, 40)
MAX_PREDICTIONS = 12000


def _num(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _closed_1m(snapshot: dict[str, Any], captured: datetime) -> list[dict[str, Any]]:
    history = snapshot.get("predictive_history") or {}
    rows = history.get("xperp_1m")
    if not isinstance(rows, list) or not rows:
        rows = (((snapshot.get("candles_1m_5m") or {}).get("ZEC-USD_UM_XPERP") or {}).get("1m") or {}).get("tail", [])
    valid = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _num(row.get("ts_ms"))
        completed = _time(row.get("completed_at"))
        vals = {key: _num(row.get(key)) for key in ("open", "high", "low", "close")}
        if ts is None or completed is None or completed > captured or any(v is None for v in vals.values()):
            continue
        if int(completed.timestamp() * 1000) != int(ts) + 60000:
            continue
        if vals["low"] <= 0 or vals["high"] < max(vals["open"], vals["close"], vals["low"]):
            continue
        if vals["low"] > min(vals["open"], vals["close"], vals["high"]):
            continue
        valid.append({**row, **vals, "ts_ms": int(ts), "_close_ms": int(completed.timestamp() * 1000)})
    valid.sort(key=lambda row: row["ts_ms"])
    dedup = {}
    for row in valid:
        dedup[row["ts_ms"]] = row
    ordered = [dedup[key] for key in sorted(dedup)]
    segment = []
    for row in ordered:
        if segment and row["ts_ms"] - segment[-1]["ts_ms"] != 60000:
            segment = []
        segment.append(row)
    return segment


def _atr(rows: list[dict[str, Any]], index: int) -> float | None:
    if index < 14:
        return None
    values = []
    for j in range(index - 13, index + 1):
        previous = rows[j - 1]["close"]
        values.append(max(rows[j]["high"] - rows[j]["low"],
                          abs(rows[j]["high"] - previous),
                          abs(rows[j]["low"] - previous)))
    value = sum(values) / len(values)
    return value if value > 0 else None


def _mature_side(entry: float, atr: float | None, side: str,
                 future: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    if not atr or side not in ("LONG", "SHORT"):
        return {"status": "N/D"}
    if len(future) != horizon:
        return {"status": "IN MATURAZIONE"}
    if any(b["_close_ms"] - a["_close_ms"] != 60000 for a, b in zip(future, future[1:])):
        return {"status": "GAP — OUTCOME N/D"}
    last = future[-1]["close"]
    signed_return = (last / entry - 1.0) * (1 if side == "LONG" else -1)
    mfe = 0.0
    mae = 0.0
    stop = entry - atr if side == "LONG" else entry + atr
    target = entry + 1.5 * atr if side == "LONG" else entry - 1.5 * atr
    barrier = "NO_BARRIER"
    hit_at = None
    for bar in future:
        if side == "LONG":
            mfe = max(mfe, (bar["high"] - entry) / atr)
            mae = max(mae, (entry - bar["low"]) / atr)
            stop_hit, target_hit = bar["low"] <= stop, bar["high"] >= target
        else:
            mfe = max(mfe, (entry - bar["low"]) / atr)
            mae = max(mae, (bar["high"] - entry) / atr)
            stop_hit, target_hit = bar["high"] >= stop, bar["low"] <= target
        if stop_hit and target_hit:
            barrier, hit_at = "AMBIGUO", bar["_close_ms"]
            break
        if stop_hit:
            barrier, hit_at = "SL", bar["_close_ms"]
            break
        if target_hit:
            barrier, hit_at = "TP1", bar["_close_ms"]
            break
    return {
        "status": "MATURO", "horizon_min": horizon,
        "directional_return_pct": round(signed_return * 100, 5),
        "mfe_atr": round(mfe, 4), "mae_atr": round(mae, 4),
        "barrier_outcome": barrier,
        "tp1_before_sl": True if barrier == "TP1" else False if barrier == "SL" else None,
        "barrier_hit_at_ms": hit_at,
        "matured_at_ms": future[-1]["_close_ms"],
        "barrier_definition": "diagnostica: SL 1 ATR, TP1 1.5 ATR; non è un setup o fill reale",
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def update_prediction_tracking(snapshot: dict[str, Any], archive_path: Path = ARCHIVE_PATH,
                               now: datetime | None = None) -> dict[str, Any]:
    captured = _time(snapshot.get("source_captured_at"))
    current = now or datetime.now(timezone.utc)
    ticker = ((snapshot.get("market") or {}).get("zec_xperp_ticker") or {}).get("first") or {}
    if (captured is None or snapshot.get("schema_version") != 3
            or snapshot.get("source_status") != "ok" or ticker.get("instId") != INSTRUMENT_ID):
        return {"status": "BLOCCATO", "reason": "snapshot non valido o strumento non esatto"}
    age = (current - captured).total_seconds()
    if age < -60 or age > 180:
        return {"status": "BLOCCATO", "reason": "snapshot stale o clock skew"}
    bars = _closed_1m(snapshot, captured)
    if len(bars) < 15:
        return {"status": "IN ATTESA", "reason": "servono almeno 15 barre 1m chiuse e continue"}
    entry_bar = bars[-1]
    entry_index = len(bars) - 1
    entry = entry_bar["close"]
    atr = _atr(bars, entry_index)
    baseline = snapshot.get("zec_early_signal") or {}
    shadow = snapshot.get("zec_shadow_signal") or {}
    baseline_side = baseline.get("candidate_direction") or baseline.get("exploratory_direction")
    if baseline_side not in ("LONG", "SHORT"):
        baseline_side = None
    shadow_side = shadow.get("candidate_direction")
    if shadow_side not in ("LONG", "SHORT"):
        shadow_side = None
    stable = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    snapshot_sha = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    prediction_id = f"{INSTRUMENT_ID}:{captured.isoformat()}:{snapshot_sha[:16]}"
    archive = {"schema_version": 1, "instrument": INSTRUMENT_ID, "updated_at": None, "predictions": []}
    if archive_path.exists():
        try:
            loaded = json.loads(archive_path.read_text(encoding="utf-8"))
            if loaded.get("schema_version") == 1 and loaded.get("instrument") == INSTRUMENT_ID:
                archive = loaded
                archive.setdefault("predictions", [])
        except Exception:
            return {"status": "BLOCCATO", "reason": "archivio preesistente non valido; preservato"}
    predictions = archive["predictions"]
    existing = next((item for item in predictions if item.get("id") == prediction_id), None)
    if existing is None:
        quality = shadow.get("data_quality") or {}
        existing = {
            "id": prediction_id, "instrument": INSTRUMENT_ID,
            "captured_at": captured.isoformat().replace("+00:00", "Z"),
            "entry_candle_completed_at": entry_bar.get("completed_at"),
            "entry_close_ms": entry_bar["_close_ms"], "entry_reference": entry,
            "atr_reference": atr, "snapshot_sha256": snapshot_sha,
            "baseline": {
                "direction": baseline_side, "decision": baseline.get("decision"),
                "status": baseline.get("status"),
            },
            "shadow": {
                "direction": shadow_side, "decision": shadow.get("decision"),
                "status": shadow.get("status"), "score": shadow.get("score"),
                "regime": (shadow.get("higher_timeframe_regime") or {}).get("combined"),
            },
            "features_available": list((shadow.get("components") or {}).keys()),
            "features_missing": list(quality.get("blockers") or []),
            "outcomes": {"baseline": {}, "shadow": {}},
        }
        predictions.append(existing)
    for engine in ("baseline", "shadow"):
        side = (existing.get(engine) or {}).get("direction")
        if side not in ("LONG", "SHORT"):
            continue
        for horizon in HORIZONS:
            key = str(horizon)
            if (existing.get("outcomes") or {}).get(engine, {}).get(key, {}).get("status") == "MATURO":
                continue
            deadline = entry_bar["_close_ms"] + horizon * 60000
            future = [bar for bar in bars if entry_bar["_close_ms"] < bar["_close_ms"] <= deadline]
            if len(future) < horizon or not future or future[-1]["_close_ms"] < deadline:
                continue
            if future[0]["_close_ms"] != entry_bar["_close_ms"] + 60000 or future[-1]["_close_ms"] != deadline:
                continue
            outcome = _mature_side(entry, atr, side, future, horizon)
            if outcome.get("status") == "MATURO":
                existing["outcomes"][engine][key] = outcome
    archive["predictions"] = predictions[-MAX_PREDICTIONS:]
    archive["updated_at"] = captured.isoformat().replace("+00:00", "Z")
    _write(archive_path, archive)
    mature = sum(
        1 for prediction in predictions
        for engine in ("baseline", "shadow")
        for outcome in (prediction.get("outcomes", {}).get(engine, {}) or {}).values()
        if outcome.get("status") == "MATURO"
    )
    return {
        "status": "OK", "prediction_id": prediction_id,
        "predictions_total": len(archive["predictions"]),
        "predictions_added": 1 if existing and existing.get("id") == prediction_id else 0,
        "mature_outcomes_total": mature,
        "snapshot_sha256": snapshot_sha,
        "instrument": INSTRUMENT_ID, "orders_enabled": False,
        "position_sizing_enabled": False,
        "note": "Tracking shadow/baseline su chiusura 1m; outcome non è fill reale né PnL. Campioni sovrapposti richiedono deduplicazione in analisi.",
    }
