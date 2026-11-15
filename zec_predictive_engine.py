#!/usr/bin/env python3
"""Explainable, simulation-only ZEC predictive engine.

Uses only completed candles for features and forward labels. It never submits orders,
never sizes positions, and never treats candle-touch as proof of a real limit fill.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
ARCHIVE_PATH = Path("data/zec_predictive_archive.json")
SCALP_HORIZONS = (5, 15, 30, 40)       # minutes
SWING_HORIZONS = (60, 120, 240)        # minutes
MAX_ARCHIVE = 12000
MIN_ANALOGS = 30
FEATURES = ("ret1", "ret3", "ret6", "atr_pct", "range_pct", "volume_rel", "rsi14", "trend")


def _num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x.replace(tzinfo=timezone.utc) if x.tzinfo is None else x.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _clean(rows: Any, seconds: int, captured: datetime | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = _num(r.get("ts_ms"))
        close_at = _dt(r.get("completed_at"))
        vals = {k: _num(r.get(k)) for k in ("open", "high", "low", "close", "volume")}
        if ts is None or close_at is None or any(vals[k] is None for k in ("open", "high", "low", "close")):
            continue
        if close_at.timestamp() * 1000 != int(ts) + seconds * 1000:
            continue
        if captured and close_at > captured:
            continue
        if vals["high"] < max(vals["open"], vals["close"], vals["low"]) or vals["low"] > min(vals["open"], vals["close"], vals["high"]):
            continue
        out.append({**r, **vals, "ts_ms": int(ts), "_close_ms": int(close_at.timestamp() * 1000)})
    out.sort(key=lambda x: x["ts_ms"])
    dedup = []
    for r in out:
        if dedup and r["ts_ms"] == dedup[-1]["ts_ms"]:
            dedup[-1] = r
        elif not dedup or r["ts_ms"] - dedup[-1]["ts_ms"] == seconds * 1000:
            dedup.append(r)
        else:
            # A gap starts a new contiguous segment; never bridge it.
            dedup = [r]
    return dedup


def _features(rows: list[dict[str, Any]], i: int) -> dict[str, float] | None:
    if i < 20:
        return None
    w = rows[i-20:i+1]
    closes = [r["close"] for r in w]
    highs = [r["high"] for r in w]
    lows = [r["low"] for r in w]
    volumes = [_num(r.get("volume")) or 0.0 for r in w]
    px = closes[-1]
    if px <= 0:
        return None
    def ret(n: int) -> float:
        return px / closes[-1-n] - 1.0 if closes[-1-n] else 0.0
    tr = []
    for j in range(1, len(w)):
        tr.append(max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1])))
    atr = sum(tr[-14:]) / min(14, len(tr))
    avgvol = sum(volumes[-20:-1]) / 20
    avg_gain = sum(max(0.0, closes[j]-closes[j-1]) for j in range(len(closes)-14, len(closes)))
    avg_loss = sum(max(0.0, closes[j-1]-closes[j]) for j in range(len(closes)-14, len(closes)))
    rsi = 50.0 if avg_gain + avg_loss == 0 else 100.0 * avg_gain / (avg_gain + avg_loss)
    return {
        "ret1": ret(1), "ret3": ret(3), "ret6": ret(6),
        "atr_pct": atr / px,
        "range_pct": (max(highs[-6:]) - min(lows[-6:])) / px,
        "volume_rel": volumes[-1] / avgvol if avgvol > 0 else 1.0,
        "rsi14": rsi / 100.0,
        "trend": (px / closes[0] - 1.0),
    }


def _label(entry_bar: dict[str, Any], future: list[dict[str, Any]], side: str,
           horizon_min: int, bar_seconds: int, fill_window_min: int) -> dict[str, Any] | None:
    px = entry_bar["close"]
    # ATR proxy from the preceding 14 completed bars is attached by caller.
    atr = _num(entry_bar.get("_atr"))
    if not atr or atr <= 0 or not future:
        return None
    pullback = 0.20 * atr
    entry = px - pullback if side == "LONG" else px + pullback
    fill_deadline = entry_bar["_close_ms"] + fill_window_min * 60_000
    horizon_deadline = entry_bar["_close_ms"] + horizon_min * 60_000
    fill_index = None
    for j, bar in enumerate(future):
        if bar["_close_ms"] > fill_deadline:
            break
        touched = bar["low"] <= entry if side == "LONG" else bar["high"] >= entry
        if touched:
            fill_index = j
            break
    if fill_index is None:
        # No touch in the defined limit window: a non-fill observation.
        if future[-1]["_close_ms"] < fill_deadline:
            return None
        return {"filled_proxy": False, "tp_before_sl": None, "entry": round(entry, 8)}
    if future[-1]["_close_ms"] < horizon_deadline:
        return None  # wait for the complete forward horizon before storing a label
    stop = entry - atr if side == "LONG" else entry + atr
    target = entry + 1.5 * atr if side == "LONG" else entry - 1.5 * atr
    outcome = None
    for bar in future[fill_index:]:
        if bar["_close_ms"] > horizon_deadline:
            break
        stop_hit = bar["low"] <= stop if side == "LONG" else bar["high"] >= stop
        target_hit = bar["high"] >= target if side == "LONG" else bar["low"] <= target
        if stop_hit and target_hit:
            outcome = False  # conservative: assume stop first within an ambiguous candle
            break
        if stop_hit:
            outcome = False
            break
        if target_hit:
            outcome = True
            break
    if outcome is None and future[-1]["_close_ms"] >= horizon_deadline:
        outcome = False  # barrier not reached inside horizon is not a TP win
    return {"filled_proxy": True, "tp_before_sl": outcome, "entry": round(entry, 8)}


def _atr(rows: list[dict[str, Any]], i: int) -> float:
    start = max(1, i-14)
    vals = []
    for j in range(start, i+1):
        prev = rows[j-1]["close"]
        vals.append(max(rows[j]["high"]-rows[j]["low"], abs(rows[j]["high"]-prev), abs(rows[j]["low"]-prev)))
    return sum(vals) / len(vals) if vals else 0.0


def _update_archive(snapshot: dict[str, Any], archive_path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    captured = _dt(snapshot.get("source_captured_at"))
    hist = snapshot.get("predictive_history") or {}
    scalp_rows = _clean(hist.get("xperp_1m"), 60, captured)
    scalp5 = _clean(hist.get("xperp_5m"), 300, captured)
    swing_rows = _clean(hist.get("xperp_1H"), 3600, captured)
    swing5 = _clean(hist.get("xperp_5m"), 300, captured)
    archive = {"schema_version": 1, "instrument": INSTRUMENT_ID, "updated_at": snapshot.get("source_captured_at"), "events": []}
    if archive_path.exists():
        try:
            loaded = json.loads(archive_path.read_text(encoding="utf-8"))
            if loaded.get("schema_version") == 1 and loaded.get("instrument") == INSTRUMENT_ID and isinstance(loaded.get("events"), list):
                archive = loaded
        except Exception:
            pass
    existing = {e.get("id"): e for e in archive["events"] if isinstance(e, dict) and e.get("id")}
    added = 0

    def process(rows: list[dict[str, Any]], future_rows: list[dict[str, Any]], interval: int,
                horizons: tuple[int, ...], family: str, fill_window: int) -> None:
        nonlocal added
        if len(rows) < 25 or len(future_rows) < 25:
            return
        # Index future bars by event close; rows are already ordered and contiguous.
        for i in range(20, len(rows)):
            feat = _features(rows, i)
            if not feat:
                continue
            bar = dict(rows[i])
            bar["_atr"] = _atr(rows, i)
            event_id = f"{family}:{bar['ts_ms']}"
            for side in ("LONG", "SHORT"):
                for horizon in horizons:
                    key = f"{event_id}:{side}:{horizon}"
                    if key in existing:
                        continue
                    future = [x for x in future_rows if x["_close_ms"] > bar["_close_ms"] and x["_close_ms"] <= bar["_close_ms"] + horizon*60_000]
                    label = _label(bar, future, side, horizon, interval, fill_window)
                    if label is None:
                        continue
                    existing[key] = {
                        "id": key, "family": family, "timestamp_ms": bar["ts_ms"],
                        "side": side, "horizon_min": horizon, "features": feat,
                        "label": label,
                    }
                    added += 1

    # Scalping features are sampled from completed 5m bars; the 1m series
    # supplies finer outcome ordering. Swing samples use completed 1H bars.
    process(scalp5, scalp_rows, 60, SCALP_HORIZONS, "scalp_5m", 5)
    process(swing_rows, swing5, 300, SWING_HORIZONS, "swing_1H", 60)
    archive["events"] = sorted(existing.values(), key=lambda x: (x.get("timestamp_ms", 0), x.get("id", "")))[-MAX_ARCHIVE:]
    archive["updated_at"] = snapshot.get("source_captured_at")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    counts = {
        "events_total": len(archive["events"]), "events_added": added,
        "scalp_1m_bars": len(scalp_rows), "scalp_5m_bars": len(scalp5),
        "swing_1h_bars": len(swing_rows),
    }
    return archive, counts


def _analogs(archive: dict[str, Any], family: str, side: str, horizon: int,
             features: dict[str, float]) -> dict[str, Any]:
    pool = [e for e in archive.get("events", [])
            if e.get("family") == family and e.get("side") == side
            and e.get("horizon_min") == horizon and isinstance(e.get("label"), dict)]
    filled = [e for e in pool if e["label"].get("filled_proxy") is True and isinstance(e["label"].get("tp_before_sl"), bool)]
    if len(pool) < MIN_ANALOGS or len(filled) < MIN_ANALOGS:
        return {"status": "IN APPRENDIMENTO", "samples": len(pool), "filled_samples": len(filled),
                "p_limit_touch_proxy": None, "p_tp_before_sl_given_touch": None,
                "reason": f"servono almeno {MIN_ANALOGS} campioni maturi e {MIN_ANALOGS} limit-touch proxy"}
    means, scales = {}, {}
    for key in FEATURES:
        vals = [_num(e.get("features", {}).get(key)) for e in pool]
        vals = [v for v in vals if v is not None]
        means[key] = sum(vals)/len(vals) if vals else 0.0
        variance = sum((v-means[key])**2 for v in vals)/len(vals) if vals else 1.0
        scales[key] = max(math.sqrt(variance), 1e-6)
    def distance(e: dict[str, Any]) -> float:
        f = e.get("features", {})
        return math.sqrt(sum(((features[k]-means[k])/scales[k] - (_num(f.get(k)) - means[k])/scales[k])**2 for k in FEATURES))
    nearest = sorted(pool, key=distance)[:min(50, len(pool))]
    nearest_filled = [e for e in nearest if e["label"].get("filled_proxy") is True]
    if len(nearest) < MIN_ANALOGS or len(nearest_filled) < MIN_ANALOGS:
        return {"status": "IN APPRENDIMENTO", "samples": len(pool), "filled_samples": len(filled),
                "p_limit_touch_proxy": None, "p_tp_before_sl_given_touch": None,
                "reason": "non ci sono abbastanza analoghi vicini con esito osservato"}
    p_touch = sum(1 for e in nearest if e["label"].get("filled_proxy") is True) / len(nearest)
    p_tp = sum(1 for e in nearest_filled if e["label"].get("tp_before_sl") is True) / len(nearest_filled)
    return {
        "status": "STIMA SPERIMENTALE", "samples": len(pool), "neighbors": len(nearest),
        "filled_neighbors": len(nearest_filled),
        "p_limit_touch_proxy": round(p_touch, 4),
        "p_tp_before_sl_given_touch": round(p_tp, 4),
        "method": "nearest-neighbor empirical frequency; not calibrated",
        "fill_proxy_caveat": "touch OHLC only: does not model queue position, spread, latency or actual execution",
        "barrier_definition": "hypothetical LIMIT offset 0.20 ATR; SL 1.0 ATR; TP1 1.5 ATR; ambiguous same-bar TP/SL counts as SL",
    }


def evaluate_predictive_engine(snapshot: dict[str, Any], archive_path: Path = ARCHIVE_PATH) -> dict[str, Any]:
    """Update the persistent event archive and return horizon-specific analog estimates."""
    if snapshot.get("market", {}).get("zec_xperp_ticker", {}).get("first", {}).get("instId") != INSTRUMENT_ID:
        return {"status": "BLOCCATO", "reason": "strumento X-Perp non esatto", "orders_enabled": False}
    archive, counts = _update_archive(snapshot, archive_path)
    hist = snapshot.get("predictive_history") or {}
    now = _dt(snapshot.get("source_captured_at"))
    scalp = _clean(hist.get("xperp_5m"), 300, now)
    swing = _clean(hist.get("xperp_1H"), 3600, now)
    result: dict[str, Any] = {
        "schema_version": 1, "engine": "ZEC Predictive Engine",
        "mode": "simulation_only", "instrument": INSTRUMENT_ID,
        "priority": "5-40m", "secondary": "1-4h",
        "archive": counts, "orders_enabled": False,
        "scalping": {"status": "DATI INSUFFICIENTI"},
        "reversals": {"status": "DATI INSUFFICIENTI"},
        "probability_note": "Nessuna probabilità viene pubblicata prima della soglia minima; le frequenze analogiche successive sono sperimentali, non calibrate.",
    }
    for rows, family, horizons, target in (
        (scalp, "scalp_5m", SCALP_HORIZONS, "scalping"),
        (swing, "swing_1H", SWING_HORIZONS, "reversals"),
    ):
        feat = _features(rows, len(rows)-1) if rows else None
        if not feat:
            result[target] = {"status": "DATI INSUFFICIENTI", "reason": "servono almeno 21 barre chiuse e continue"}
            continue
        side_results = {}
        for side in ("LONG", "SHORT"):
            side_results[side] = {str(h): _analogs(archive, family, side, h, feat) for h in horizons}
        result[target] = {
            "status": "IN APPRENDIMENTO" if all(v["status"] == "IN APPRENDIMENTO" for d in side_results.values() for v in d.values()) else "STIME SPERIMENTALI",
            "features": feat, "by_direction": side_results,
        }
    # Safety is explicit and deliberately duplicated at the engine boundary.
    result["safety"] = {
        "orders_enabled": False, "position_sizing_enabled": False,
        "ctVal_status": "NON RISOLTO", "max_risk_usdc": 15,
        "instrument_id_pinned": INSTRUMENT_ID,
    }
    return result
