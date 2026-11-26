#!/usr/bin/env python3
"""Explainable, simulation-only ZEC predictive engine.

Uses only completed candles for features and forward labels. It never submits orders,
never sizes positions, and never treats candle-touch as proof of a real limit fill.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zec_queue_model import estimate_queue_depletion
from zec_contract_config import build_contract_safety

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
ARCHIVE_PATH = Path("data/zec_predictive_archive.json")
SCALP_HORIZONS = (5, 15, 30, 40)       # minutes
SWING_HORIZONS = (60, 120, 240)        # minutes
LIMIT_OFFSETS_ATR = (0.10, 0.20, 0.30, 0.40)
MAX_ARCHIVE = 12000
MIN_ANALOGS = 30
FEATURES = ("ret1", "ret3", "ret6", "atr_pct", "range_pct", "volume_rel", "rsi14", "trend", "rel_strength", "btc_trend_20")
MICRO_FEATURES = FEATURES + ("cvd1m_log", "cvd5m_log", "cvd15m_log", "imbalance_top10", "book_delta_top10", "book_delta_valid", "spread_bps", "oi_change_pct", "oi_change_valid")
QUEUE_FEATURES = FEATURES + ("imbalance_top10", "book_delta_top10", "book_delta_valid", "spread_bps", "oi_change_pct", "oi_change_valid")
CONTEXT_ARCHIVE_PATH = Path("data/zec_context_archive.json")
QUEUE_HISTORY_PATH = Path("data/zec_microstructure_history.json")


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
        if int(ts) % (seconds * 1000):
            continue
        if vals["high"] < max(vals["open"], vals["close"], vals["low"]) or vals["low"] > min(vals["open"], vals["close"], vals["high"]):
            continue
        if vals["low"] <= 0:
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


def _features(rows: list[dict[str, Any]], i: int, benchmark: dict[int, float] | None = None) -> dict[str, float] | None:
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
    volume_window = volumes[-20:-1]
    avgvol = sum(volume_window) / len(volume_window) if volume_window else 0.0
    avg_gain = sum(max(0.0, closes[j]-closes[j-1]) for j in range(len(closes)-14, len(closes)))
    avg_loss = sum(max(0.0, closes[j-1]-closes[j]) for j in range(len(closes)-14, len(closes)))
    rsi = 50.0 if avg_gain + avg_loss == 0 else 100.0 * avg_gain / (avg_gain + avg_loss)
    relative_strength = 0.0
    btc_trend_20 = None
    if benchmark:
        current_btc = benchmark.get(rows[i]["ts_ms"])
        prior_btc = benchmark.get(rows[i-3]["ts_ms"])
        prior_btc_20 = benchmark.get(rows[i-20]["ts_ms"])
        if current_btc and prior_btc:
            relative_strength = ret(3) - (current_btc / prior_btc - 1.0)
        # BTC context is explicit and horizon-matched to this model family.
        # Missing benchmark alignment stays missing; it is never imputed as flat.
        if current_btc and prior_btc_20:
            btc_trend_20 = current_btc / prior_btc_20 - 1.0
    return {
        "ret1": ret(1), "ret3": ret(3), "ret6": ret(6),
        "atr_pct": atr / px,
        "range_pct": (max(highs[-6:]) - min(lows[-6:])) / px,
        "volume_rel": volumes[-1] / avgvol if avgvol > 0 else 1.0,
        "rsi14": rsi / 100.0,
        "trend": (px / closes[0] - 1.0),
        "rel_strength": relative_strength,
        "btc_trend_20": btc_trend_20,
    }


def _label(entry_bar: dict[str, Any], future: list[dict[str, Any]], side: str,
           horizon_min: int, bar_seconds: int, fill_window_min: int,
           limit_price: float | None = None) -> dict[str, Any] | None:
    px = entry_bar["close"]
    # ATR proxy from the preceding 14 completed bars is attached by caller.
    atr = _num(entry_bar.get("_atr"))
    if not atr or atr <= 0 or not future:
        return None
    pullback = 0.20 * atr
    entry = limit_price if limit_price is not None else (px - pullback if side == "LONG" else px + pullback)
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
        return {"filled_proxy": False, "tp_before_sl": None, "entry": round(entry, 8),
                "fill_window_completed_at_ms": future[-1]["_close_ms"]}
    # A filled observation may be stored with an unresolved TP/SL outcome;
    # later runs mature it when the full forward horizon becomes available.
    stop = entry - atr if side == "LONG" else entry + atr
    target = entry + 1.5 * atr if side == "LONG" else entry - 1.5 * atr
    outcome = None
    outcome_at_ms = None
    for bar in future[fill_index:]:
        if bar["_close_ms"] > horizon_deadline:
            break
        stop_hit = bar["low"] <= stop if side == "LONG" else bar["high"] >= stop
        target_hit = bar["high"] >= target if side == "LONG" else bar["low"] <= target
        if stop_hit and target_hit:
            outcome = False  # conservative: assume stop first within an ambiguous candle
            outcome_at_ms = bar["_close_ms"]
            break
        if stop_hit:
            outcome = False
            outcome_at_ms = bar["_close_ms"]
            break
        if target_hit:
            outcome = True
            outcome_at_ms = bar["_close_ms"]
            break
    if outcome is None and future[-1]["_close_ms"] >= horizon_deadline:
        outcome = False  # barrier not reached inside horizon is not a TP win
        outcome_at_ms = future[-1]["_close_ms"]
    return {"filled_proxy": True, "tp_before_sl": outcome, "entry": round(entry, 8),
            "outcome_at_ms": outcome_at_ms}


def _atr(rows: list[dict[str, Any]], i: int) -> float:
    start = max(1, i-14)
    vals = []
    for j in range(start, i+1):
        prev = rows[j-1]["close"]
        vals.append(max(rows[j]["high"]-rows[j]["low"], abs(rows[j]["high"]-prev), abs(rows[j]["low"]-prev)))
    return sum(vals) / len(vals) if vals else 0.0


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Preserve the last valid archive if a write is interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _update_archive(snapshot: dict[str, Any], archive_path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    captured = _dt(snapshot.get("source_captured_at"))
    hist = snapshot.get("predictive_history") or {}
    scalp_rows = _clean(hist.get("xperp_1m"), 60, captured)
    scalp5 = _clean(hist.get("xperp_5m"), 300, captured)
    swing_rows = _clean(hist.get("xperp_1H"), 3600, captured)
    swing5 = _clean(hist.get("xperp_5m"), 300, captured)
    btc5 = _clean(hist.get("btc_5m"), 300, captured)
    btc_by_ts = {r["ts_ms"]: r["close"] for r in btc5}
    btc1h = _clean(hist.get("btc_1H"), 3600, captured)
    btc1h_by_ts = {r["ts_ms"]: r["close"] for r in btc1h}
    try:
        context_archive = json.loads(CONTEXT_ARCHIVE_PATH.read_text(encoding="utf-8"))
        context_observations = context_archive.get("observations", [])
    except Exception:
        context_observations = []
    context_observations = sorted(
        [o for o in context_observations if isinstance(o, dict) and _num(o.get("minute_ms")) is not None],
        key=lambda o: o["minute_ms"]
    )
    try:
        queue_history = json.loads(QUEUE_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        queue_history = {}
    queue_book_times = [
        int(_num(row.get("ts_ms")) or 0)
        for row in queue_history.get("book_snapshots", [])
        if isinstance(row, dict) and row.get("source") == "websocket" and _num(row.get("ts_ms")) is not None
    ]
    queue_book_start_ms = min(queue_book_times) if queue_book_times else None
    queue_book_end_ms = max(queue_book_times) if queue_book_times else None
    instrument_meta = ((snapshot.get("market") or {}).get("zec_xperp_instrument") or {}).get("first") or {}
    tick_size = _num(instrument_meta.get("tickSz")) if instrument_meta.get("instId") == INSTRUMENT_ID else None
    archive = {"schema_version": 1, "instrument": INSTRUMENT_ID, "updated_at": snapshot.get("source_captured_at"), "events": []}
    if archive_path.exists():
        try:
            loaded = json.loads(archive_path.read_text(encoding="utf-8"))
            if loaded.get("schema_version") == 1 and loaded.get("instrument") == INSTRUMENT_ID and isinstance(loaded.get("events"), list):
                archive = loaded
        except Exception:
            pass
    existing = {e.get("id"): e for e in archive["events"] if isinstance(e, dict) and e.get("id")}
    # Schema correction: swing samples have no paired BTC 1H series yet.
    # Never reuse ZEC momentum as if it were relative strength.
    for event in existing.values():
        if event.get("family") == "swing_1H" and isinstance(event.get("features"), dict):
            event["features"]["rel_strength"] = 0.0
    added = 0

    def process(rows: list[dict[str, Any]], future_rows: list[dict[str, Any]], interval: int,
                horizons: tuple[int, ...], family: str, fill_window: int,
                benchmark: dict[int, float] | None = None) -> None:
        nonlocal added
        if len(rows) < 25 or len(future_rows) < 25:
            return
        # Index future bars by event close; rows are already ordered and contiguous.
        for i in range(20, len(rows)):
            feat = _features(rows, i, benchmark)
            if not feat:
                continue
            bar = dict(rows[i])
            bar["_atr"] = _atr(rows, i)
            event_id = f"{family}:{bar['ts_ms']}"
            for side in ("LONG", "SHORT"):
                for horizon in horizons:
                    key = f"{event_id}:{side}:{horizon}"
                    future = [x for x in future_rows if x["_close_ms"] > bar["_close_ms"] and x["_close_ms"] <= bar["_close_ms"] + horizon*60_000]
                    if key in existing:
                        old_label = existing[key].get("label", {})
                        if old_label.get("filled_proxy") is True and old_label.get("tp_before_sl") is None:
                            matured_label = _label(bar, future, side, horizon, interval, fill_window, _num(old_label.get("entry")))
                            if matured_label and isinstance(matured_label.get("tp_before_sl"), bool):
                                old_label["tp_before_sl"] = matured_label["tp_before_sl"]
                                old_label["matured_at_ms"] = matured_label.get("outcome_at_ms")
                        continue
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
    process(scalp5, scalp_rows, 60, SCALP_HORIZONS, "scalp_5m", 5, btc_by_ts)
    process(swing_rows, swing5, 300, SWING_HORIZONS, "swing_1H", 60, btc1h_by_ts)

    def nearest_context(close_ms: int) -> dict[str, Any] | None:
        candidates = [o for o in context_observations
                      if _dt(o.get("captured_at")) is not None
                      and 0 <= close_ms - int(_dt(o["captured_at"]).timestamp() * 1000) <= 90000]
        return candidates[-1] if candidates else None

    def nearest_context_any(close_ms: int, tolerance_ms: int) -> dict[str, Any] | None:
        candidates = []
        for observation in context_observations:
            captured_at = _dt(observation.get("captured_at"))
            if captured_at is None:
                continue
            observed_ms = int(captured_at.timestamp() * 1000)
            delta = abs(observed_ms - close_ms)
            if delta <= tolerance_ms:
                candidates.append((delta, observed_ms, observation))
        return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    def enrich_queue(base: dict[str, float], obs: dict[str, Any] | None,
                     prior: dict[str, Any] | None) -> dict[str, float] | None:
        if not obs:
            return None
        imbalance, spread = _num(obs.get("imbalance_top10")), _num(obs.get("spread_bps"))
        if imbalance is None or spread is None:
            return None
        oi_delta = 0.0
        if prior:
            old_oi, new_oi = _num(prior.get("oi_contracts")), _num(obs.get("oi_contracts"))
            if old_oi and new_oi is not None:
                oi_delta = new_oi / old_oi - 1.0
        return {
            **base, "imbalance_top10": imbalance,
            "book_delta_top10": _num(obs.get("book_delta_top10")) or 0.0,
            "book_delta_valid": 1.0 if obs.get("book_delta_valid") else 0.0,
            "spread_bps": spread, "oi_change_pct": oi_delta,
            "oi_change_valid": 1.0 if (prior and prior.get("oi_valid") and obs.get("oi_valid")) else 0.0,
        }

    def enrich(base: dict[str, float], obs: dict[str, Any] | None,
               prior: dict[str, Any] | None) -> dict[str, float] | None:
        if not obs:
            return None
        values = [_num(obs.get(k)) for k in (
            "cvd_1m_contracts", "cvd_5m_contracts", "cvd_15m_contracts",
            "imbalance_top10", "spread_bps")]
        if any(value is None for value in values):
            return None
        cvd1, cvd5, cvd15, imbalance, spread = values
        signed_log = lambda value: math.copysign(math.log1p(abs(value)), value)
        oi_delta = 0.0
        if prior:
            old_oi, new_oi = _num(prior.get("oi_contracts")), _num(obs.get("oi_contracts"))
            if old_oi and new_oi is not None:
                oi_delta = new_oi / old_oi - 1.0
        return {
            **base,
            "cvd1m_log": signed_log(cvd1), "cvd5m_log": signed_log(cvd5),
            "cvd15m_log": signed_log(cvd15), "imbalance_top10": imbalance,
            "book_delta_top10": _num(obs.get("book_delta_top10")) or 0.0,
            "book_delta_valid": 1.0 if obs.get("book_delta_valid") else 0.0,
            "spread_bps": spread, "oi_change_pct": oi_delta,
            "oi_change_valid": 1.0 if (prior and prior.get("oi_valid") and obs.get("oi_valid")) else 0.0,
        }

    def process_micro(rows: list[dict[str, Any]], future_rows: list[dict[str, Any]],
                      horizons: tuple[int, ...], family: str,
                      benchmark: dict[int, float] | None, prior_minutes: int) -> None:
        nonlocal added
        if len(rows) < 25 or len(future_rows) < 25 or len(context_observations) < 2:
            return
        for i in range(20, len(rows)):
            base = _features(rows, i, benchmark)
            if not base:
                continue
            close_ms = rows[i]["_close_ms"]
            obs = nearest_context(close_ms)
            if not obs:
                continue
            prior = nearest_context(close_ms - prior_minutes * 60000)
            feat = enrich_queue(base, obs, prior)
            if not feat:
                continue
            bar = dict(rows[i])
            bar["_atr"] = _atr(rows, i)
            event_id = f"{family}:{bar['ts_ms']}"
            interval = 60 if family.startswith("scalp") else 300
            fill_window = 5 if family.startswith("scalp") else 60
            for side in ("LONG", "SHORT"):
                for horizon in horizons:
                    key = f"{event_id}:{side}:{horizon}"
                    future = [x for x in future_rows if close_ms < x["_close_ms"] <= close_ms + horizon * 60000]
                    if key in existing:
                        old_label = existing[key].get("label", {})
                        if old_label.get("filled_proxy") is True and old_label.get("tp_before_sl") is None:
                            matured_label = _label(bar, future, side, horizon, interval, fill_window, _num(old_label.get("entry")))
                            if matured_label and isinstance(matured_label.get("tp_before_sl"), bool):
                                old_label["tp_before_sl"] = matured_label["tp_before_sl"]
                                old_label["matured_at_ms"] = matured_label.get("outcome_at_ms")
                        continue
                    label = _label(bar, future, side, horizon, interval, fill_window)
                    if label is None:
                        continue
                    existing[key] = {
                        "id": key, "family": family, "timestamp_ms": bar["ts_ms"],
                        "side": side, "horizon_min": horizon, "features": feat,
                        "label": label, "micro_observation_minute_ms": obs.get("minute_ms"),
                    }
                    added += 1

    process_micro(scalp5, scalp_rows, SCALP_HORIZONS, "scalp_micro_5m", btc_by_ts, 5)
    process_micro(swing_rows, swing5, SWING_HORIZONS, "swing_micro_1H", btc1h_by_ts, 60)

    def process_queue(rows: list[dict[str, Any]], future_rows: list[dict[str, Any]]) -> None:
        nonlocal added
        if len(rows) < 25 or len(future_rows) < 25 or tick_size is None or queue_history.get("instrument") != INSTRUMENT_ID:
            return
        for i in range(20, len(rows)):
            base = _features(rows, i, btc_by_ts)
            if not base:
                continue
            close_ms = rows[i]["_close_ms"]
            # Context capture may land shortly after a completed 5m close.
            # Align to the nearest observation within 90s and start labels
            # only after that observation to avoid using pre-observation moves.
            obs = nearest_context_any(close_ms, 90000)
            if not obs:
                continue
            obs_ms = int(_dt(obs["captured_at"]).timestamp() * 1000)
            prior = nearest_context(obs_ms - 5*60000)
            feat = enrich_queue(base, obs, prior)
            if not feat:
                continue
            bar = dict(rows[i])
            bar["_close_ms"] = max(close_ms, obs_ms)
            event_ms = bar["_close_ms"]
            bar["_atr"] = _atr(rows, i)
            if not bar["_atr"] or bar["_atr"] <= 0:
                continue
            for side in ("LONG", "SHORT"):
                for offset in LIMIT_OFFSETS_ATR:
                    family = f"scalp_queue_{int(offset*100):02d}"
                    # Mature TP/SL outcomes for previously recorded queue fills,
                    # even after their original book/trade window has aged out.
                    for horizon in SCALP_HORIZONS:
                        key = f"{family}:{bar['ts_ms']}:{side}:{horizon}"
                        if key not in existing:
                            continue
                        old_label = existing[key].get("label", {})
                        if old_label.get("filled_proxy") is True and old_label.get("tp_before_sl") is None:
                            future = [x for x in future_rows if event_ms < x["_close_ms"] <= event_ms + horizon*60000]
                            matured_label = _label(bar, future, side, horizon, 60, 5, _num(old_label.get("entry")))
                            if matured_label and matured_label.get("filled_proxy") and isinstance(matured_label.get("tp_before_sl"), bool):
                                old_label["tp_before_sl"] = matured_label["tp_before_sl"]
                                old_label["matured_at_ms"] = matured_label.get("outcome_at_ms")
                        continue
                    if (queue_book_start_ms is None or queue_book_end_ms is None
                            or event_ms < queue_book_start_ms - 2000
                            or event_ms + 5*60000 > queue_book_end_ms):
                        continue
                    raw_limit = bar["close"] - offset*bar["_atr"] if side == "LONG" else bar["close"] + offset*bar["_atr"]
                    queue_info = estimate_queue_depletion(queue_history, event_ms, raw_limit, side, tick_size, 5)
                    if queue_info.get("status") != "STIMA QUEUE DISPONIBILE":
                        continue
                    for horizon in SCALP_HORIZONS:
                        key = f"{family}:{bar['ts_ms']}:{side}:{horizon}"
                        if key in existing:
                            continue
                        future = [x for x in future_rows if event_ms < x["_close_ms"] <= event_ms + horizon*60000]
                        label = _label(bar, future, side, horizon, 60, 5, queue_info["limit_price"])
                        if label is None:
                            continue
                        if queue_info["queue_depleted_proxy"]:
                            if not label.get("filled_proxy"):
                                continue
                        else:
                            label["filled_proxy"] = False
                            label["tp_before_sl"] = None
                        label["queue_info"] = queue_info
                        label["limit_offset_atr"] = offset
                        existing[key] = {
                            "id": key, "family": family,
                            "timestamp_ms": event_ms, "side": side,
                            "horizon_min": horizon, "features": feat, "label": label,
                        }
                        added += 1

    process_queue(scalp5, scalp_rows)
    archive["events"] = sorted(existing.values(), key=lambda x: (x.get("timestamp_ms", 0), x.get("id", "")))[-MAX_ARCHIVE:]
    archive["updated_at"] = snapshot.get("source_captured_at")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(archive_path, archive)
    counts = {
        "events_total": len(archive["events"]), "events_added": added,
        "scalp_1m_bars": len(scalp_rows), "scalp_5m_bars": len(scalp5),
        "swing_1h_bars": len(swing_rows),
    }
    return archive, counts


def _analogs(archive: dict[str, Any], family: str, side: str, horizon: int,
             features: dict[str, float], feature_names: tuple[str, ...] = FEATURES,
             as_of_ms: int | None = None) -> dict[str, Any]:
    pool = [e for e in archive.get("events", [])
            if e.get("family") == family and e.get("side") == side
            and e.get("horizon_min") == horizon and isinstance(e.get("label"), dict)]
    archive_count = len(pool)
    overlap_excluded = 0
    immature_excluded = 0
    if as_of_ms is not None:
        # Only use observations whose complete forward-label window ended
        # before the current forecast. This is a strict temporal embargo.
        mature_pool = []
        for event in pool:
            stamp = _num(event.get("timestamp_ms"))
            label = event.get("label") or {}
            if stamp is None:
                immature_excluded += 1
                continue
            # OHLC families store candle-open timestamps; queue samples store
            # their aligned observation timestamp (already the label origin).
            if family in ("scalp_5m", "scalp_micro_5m"):
                origin_ms = stamp + 5 * 60_000
            elif family in ("swing_1H", "swing_micro_1H"):
                origin_ms = stamp + 60 * 60_000
            else:
                origin_ms = stamp
            if origin_ms + horizon * 60_000 > as_of_ms:
                immature_excluded += 1
                continue
            if label.get("filled_proxy") is True:
                mature = isinstance(label.get("tp_before_sl"), bool)
            else:
                fill_end = _num(label.get("fill_window_completed_at_ms"))
                mature = fill_end is not None and fill_end <= as_of_ms
            if not mature:
                immature_excluded += 1
                continue
            mature_pool.append(event)

        # Greedy newest-first sampling: retain the most recent independent
        # forward windows and discard events whose label windows overlap.
        selected = []
        next_allowed_start = float("inf")
        for event in sorted(mature_pool, key=lambda item: _num(item.get("timestamp_ms")) or 0, reverse=True):
            stamp = int(_num(event.get("timestamp_ms")) or 0)
            if family in ("scalp_5m", "scalp_micro_5m"):
                origin_ms = stamp + 5 * 60_000
            elif family in ("swing_1H", "swing_micro_1H"):
                origin_ms = stamp + 60 * 60_000
            else:
                origin_ms = stamp
            end = origin_ms + horizon * 60_000
            if end <= next_allowed_start:
                selected.append(event)
                next_allowed_start = origin_ms
            else:
                overlap_excluded += 1
        pool = list(reversed(selected))

    # Historical archives can contain older or partially populated feature
    # schemas. Keep those observations in the archive, but never pass None
    # into the distance calculation or silently impute missing values.
    current_vector = {key: _num(features.get(key)) for key in feature_names}
    valid_current = all(value is not None for value in current_vector.values())
    valid_pool = []
    for event in pool:
        stored = event.get("features")
        if not isinstance(stored, dict):
            continue
        vector = {key: _num(stored.get(key)) for key in feature_names}
        if all(value is not None for value in vector.values()):
            valid_pool.append((event, vector))

    # Regime-aware analog selection: separate directional structure from
    # volatility conditions. The trend score is normalized by recent ATR so
    # thresholds adapt to the instrument's current volatility.
    regime_name = None
    regime_pool = []
    volatility_cut = None
    if valid_current and valid_pool:
        atr_values = sorted(vector["atr_pct"] for _, vector in valid_pool)
        volatility_cut = atr_values[len(atr_values) // 2]

        def regime_key(vector: dict[str, float]) -> tuple[str, str]:
            normalized_trend = vector["trend"] / max(
                vector["atr_pct"] * math.sqrt(20.0), 1e-6
            )
            if normalized_trend >= 0.75:
                trend_regime = "UP"
            elif normalized_trend <= -0.75:
                trend_regime = "DOWN"
            else:
                trend_regime = "RANGE"
            volatility_regime = "HIGH_VOL" if vector["atr_pct"] >= volatility_cut else "LOW_VOL"
            return trend_regime, volatility_regime

        regime_name = "/".join(regime_key(current_vector))
        regime_pool = [
            item for item in valid_pool
            if regime_key(item[1]) == tuple(regime_name.split("/"))
        ]

    # Do not create a fragile tiny-sample model: fall back to the broad pool
    # until the current regime has at least MIN_ANALOGS complete observations.
    regime_fallback = len(regime_pool) < MIN_ANALOGS
    model_pool = valid_pool if regime_fallback else regime_pool
    filled_proxy_pool = [e for e, _ in model_pool if e["label"].get("filled_proxy") is True]
    filled_pool = [e for e in filled_proxy_pool if isinstance(e["label"].get("tp_before_sl"), bool)]
    base = {
        "samples": len(model_pool),
        "regime": regime_name,
        "regime_samples": len(regime_pool),
        "regime_fallback_to_broad_pool": regime_fallback,
        "regime_volatility_cut_atr_pct": round(volatility_cut, 8) if volatility_cut is not None else None,
        "archive_samples": archive_count,
        "temporal_samples": len(pool),
        "overlap_excluded_samples": overlap_excluded,
        "immature_excluded_samples": immature_excluded,
        "incomplete_feature_samples": len(pool) - len(valid_pool),
        "filled_samples": len(filled_pool),
        "filled_proxy_samples": len(filled_proxy_pool),
        "neighbors": min(50, len(valid_pool)),
        "filled_neighbors": 0,
        "filled_proxy_neighbors": 0,
        "p_limit_touch_proxy": None,
        "p_tp_before_sl_given_touch": None,
        "tp_status": "IN APPRENDIMENTO",
    }
    if not valid_current:
        return {
            **base, "status": "IN APPRENDIMENTO",
            "reason": "feature correnti incomplete; probabilità non calcolata",
        }
    if len(valid_pool) < MIN_ANALOGS:
        return {
            **base, "status": "IN APPRENDIMENTO",
            "reason": f"servono almeno {MIN_ANALOGS} osservazioni con feature complete per stimare il tocco",
        }

    means, scales = {}, {}
    for key in feature_names:
        vals = [vector[key] for _, vector in model_pool]
        means[key] = sum(vals) / len(vals)
        variance = sum((value - means[key]) ** 2 for value in vals) / len(vals)
        scales[key] = max(math.sqrt(variance), 1e-6)

    def distance(item: tuple[dict[str, Any], dict[str, float]]) -> float:
        _, vector = item
        return math.sqrt(sum(
            ((current_vector[key] - vector[key]) / scales[key]) ** 2
            for key in feature_names
        ))

    nearest_items = sorted(model_pool, key=distance)[:min(50, len(model_pool))]
    nearest = [event for event, _ in nearest_items]
    if len(nearest) < MIN_ANALOGS:
        return {
            **base, "status": "IN APPRENDIMENTO",
            "neighbors": len(nearest),
            "reason": "non ci sono abbastanza analoghi vicini con feature complete",
        }
    nearest_filled_proxy = [e for e in nearest if e["label"].get("filled_proxy") is True]
    nearest_filled = [e for e in nearest_filled_proxy if isinstance(e["label"].get("tp_before_sl"), bool)]
    p_touch = len(nearest_filled_proxy) / len(nearest)
    p_tp = None
    tp_status = "IN APPRENDIMENTO"
    if len(nearest_filled) >= MIN_ANALOGS:
        p_tp = sum(1 for e in nearest_filled if e["label"].get("tp_before_sl") is True) / len(nearest_filled)
        tp_status = "STIMA SPERIMENTALE"
    return {
        **base, "status": "STIMA SPERIMENTALE",
        "neighbors": len(nearest),
        "filled_neighbors": len(nearest_filled),
        "filled_proxy_neighbors": len(nearest_filled_proxy),
        "p_limit_touch_proxy": round(p_touch, 4),
        "p_tp_before_sl_given_touch": round(p_tp, 4) if p_tp is not None else None,
        "tp_status": tp_status,
        "method": "nearest-neighbor empirical frequency; not calibrated",
        "fill_proxy_caveat": "OHLC touch or queue depletion proxy depending on sample family; neither proves actual execution",
        "barrier_definition": "hypothetical LIMIT offset; SL 1.0 ATR; TP1 1.5 ATR; ambiguous same-bar TP/SL counts as SL",
    }


def evaluate_predictive_engine(snapshot: dict[str, Any], archive_path: Path = ARCHIVE_PATH) -> dict[str, Any]:
    """Update the persistent event archive and return horizon-specific analog estimates."""
    market_records = snapshot.get("market", {}) if isinstance(snapshot.get("market"), dict) else {}
    ticker = market_records.get("zec_xperp_ticker", {})
    first = ticker.get("first", {}) if isinstance(ticker, dict) else {}
    instrument_record = market_records.get("zec_xperp_instrument", {})
    if not isinstance(instrument_record, dict):
        instrument_record = {}
    instrument = instrument_record.get("first", {})
    if not isinstance(instrument, dict):
        instrument = {}
    instrument_age = instrument_record.get("exchange_age_seconds")
    if instrument_age is None:
        instrument_age = instrument_record.get("age_seconds")
    try:
        instrument_fresh = instrument_age is not None and -1 <= float(instrument_age) <= 180
    except (TypeError, ValueError):
        instrument_fresh = False
    instrument_verified = (
        instrument_record.get("http_status") == 200
        and str(instrument_record.get("api_code")) == "0"
        and instrument_fresh
        and instrument.get("instId") == INSTRUMENT_ID
    )
    contract_safety = build_contract_safety(instrument, verified=instrument_verified)
    if first.get("instId") != INSTRUMENT_ID:
        return {
            "status": "BLOCCATO", "reason": "strumento X-Perp non esatto",
            "archive": {"events_total": 0, "events_added": 0},
            "scalping": {"status": "BLOCCATO"}, "reversals": {"status": "BLOCCATO"},
            "orders_enabled": False,
            "safety": {
                **contract_safety, "orders_enabled": False,
                "position_sizing_enabled": False, "instrument_id_pinned": INSTRUMENT_ID,
            },
        }
    captured = _dt(snapshot.get("source_captured_at"))
    age = (datetime.now(timezone.utc) - captured).total_seconds() if captured else None
    history_quality = snapshot.get("zec_xperp_history") or {}
    predictive_history = snapshot.get("predictive_history") or {}
    scalp_1m_check = _clean(predictive_history.get("xperp_1m"), 60, captured)
    scalp_5m_check = _clean(predictive_history.get("xperp_5m"), 300, captured)
    scalp_1m_age = (captured.timestamp()*1000 - scalp_1m_check[-1]["_close_ms"]) if captured and scalp_1m_check else None
    scalp_5m_age = (captured.timestamp()*1000 - scalp_5m_check[-1]["_close_ms"]) if captured and scalp_5m_check else None
    required_history = ("1m", "5m", "1H", "4H")
    blockers = []
    if snapshot.get("schema_version") != 3 or snapshot.get("source_status") != "ok":
        blockers.append("snapshot schema/status non valido")
    source_mode = snapshot.get("feed_source_mode")
    if source_mode in ("OKX_EEA_REST_SNAPSHOT", "OKX_EEA_REST_WS_SNAPSHOT", "LEGACY_RENDER_WEBSOCKET"):
        if snapshot.get("feed_capture_status") != "ok":
            blockers.append("acquisizione feed non valida")
        health = snapshot.get("feed_health") or {}
        if health.get("status") != "ok":
            blockers.append("diagnostica del feed non valida")
        if source_mode == "OKX_EEA_REST_SNAPSHOT":
            blockers.append("microstruttura WebSocket continua N/D: snapshot REST non prova continuità LIVE")
        elif health.get("websocket_connected") is not True:
            blockers.append("feed WebSocket non connesso o health non OK")
    else:
        if snapshot.get("render_health_http") != 200 or snapshot.get("render_candles_http") != 200:
            blockers.append("healthz/candles non entrambi HTTP 200")
        health = snapshot.get("render_health") or {}
        if health.get("status") != "ok" or health.get("websocket_connected") is not True:
            blockers.append("feed WebSocket non connesso o health non OK")
    if age is None or age < 0 or age > 180:
        blockers.append("snapshot oltre 180 secondi o timestamp non valido")
    if ticker.get("http_status") != 200 or str(ticker.get("api_code")) != "0":
        blockers.append("ticker X-Perp HTTP/API non valido")
    if any(not (history_quality.get(k) or {}).get("ready_200") for k in required_history):
        blockers.append("storico X-Perp 1m/5m/1H/4H non pronto (200 barre, zero gap, fresco)")
    if len(scalp_1m_check) < 200 or scalp_1m_age is None or scalp_1m_age > 180000:
        blockers.append("serie OKX X-Perp 1m insufficiente o non fresca (servono 200 barre e ultima chiusura <=180s)")
    if len(scalp_5m_check) < 200 or scalp_5m_age is None or scalp_5m_age > 360000:
        blockers.append("serie OKX X-Perp 5m insufficiente o non fresca (servono 200 barre e ultima chiusura <=360s)")
    if any(v for v in (snapshot.get("read_errors") or {}).values()):
        blockers.append("errori di lettura nel bridge")
    if blockers:
        return {
            "status": "BLOCCATO", "reason": "; ".join(blockers),
            "archive": {"events_total": 0, "events_added": 0},
            "scalping": {"status": "BLOCCATO"}, "reversals": {"status": "BLOCCATO"},
            "orders_enabled": False, "archive_updated": False,
            "safety": {
                **contract_safety,
                "orders_enabled": False,
                "position_sizing_enabled": False,
                "instrument_id_pinned": INSTRUMENT_ID,
            },
        }
    archive, counts = _update_archive(snapshot, archive_path)
    hist = snapshot.get("predictive_history") or {}
    now = _dt(snapshot.get("source_captured_at"))
    scalp = _clean(hist.get("xperp_5m"), 300, now)
    btc5 = _clean(hist.get("btc_5m"), 300, now)
    btc_by_ts = {r["ts_ms"]: r["close"] for r in btc5}
    swing = _clean(hist.get("xperp_1H"), 3600, now)
    btc1h = _clean(hist.get("btc_1H"), 3600, now)
    btc1h_by_ts = {r["ts_ms"]: r["close"] for r in btc1h}
    four_hour = _clean(hist.get("xperp_4H"), 14400, now)
    four_hour_context = _features(four_hour, len(four_hour)-1) if four_hour else None
    result: dict[str, Any] = {
        "schema_version": 1, "engine": "ZEC Predictive Engine",
        "mode": "simulation_only", "instrument": INSTRUMENT_ID,
        "priority": "5-40m", "secondary": "1-4h",
        "archive": counts, "orders_enabled": False,
        "scalping": {"status": "DATI INSUFFICIENTI"},
        "reversals": {"status": "DATI INSUFFICIENTI"},
        "probability_note": "Frequenze analogiche sperimentali, non calibrate; eventi contigui e sovrapposti non equivalgono a trade indipendenti o setup attivati.",
        "sample_warning": "Il conteggio rappresenta barre/eventi storici analoghi, non operazioni reali indipendenti. Non è un win-rate né prova di edge.",
    }
    for rows, family, horizons, target in (
        (scalp, "scalp_5m", SCALP_HORIZONS, "scalping"),
        (swing, "swing_1H", SWING_HORIZONS, "reversals"),
    ):
        feat = _features(rows, len(rows)-1, btc_by_ts if target == "scalping" else btc1h_by_ts) if rows else None
        if not feat:
            result[target] = {"status": "DATI INSUFFICIENTI", "reason": "servono almeno 21 barre chiuse e continue"}
            continue
        side_results = {}
        for side in ("LONG", "SHORT"):
            side_results[side] = {str(h): _analogs(archive, family, side, h, feat, as_of_ms=rows[-1]["_close_ms"]) for h in horizons}
        result[target] = {
            "status": "IN APPRENDIMENTO" if all(v["status"] == "IN APPRENDIMENTO" for d in side_results.values() for v in d.values()) else "STIME SPERIMENTALI",
            "features": feat, "by_direction": side_results,
        }
    # Separate microstructure-enhanced analogs; no mixing with OHLC-only samples.
    try:
        context_data = json.loads(CONTEXT_ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        context_data = {}
    observations = sorted(context_data.get("observations", []), key=lambda o: o.get("minute_ms", 0))

    # Evaluator-local helpers; _update_archive helpers are not in this scope.
    def nearest_context(close_ms: int) -> dict[str, Any] | None:
        candidates = [
            o for o in observations
            if _dt(o.get("captured_at")) is not None
            and 0 <= close_ms - int(_dt(o["captured_at"]).timestamp() * 1000) <= 90000
        ]
        return candidates[-1] if candidates else None

    def enrich_queue(base: dict[str, float], obs: dict[str, Any] | None,
                     prior: dict[str, Any] | None) -> dict[str, float] | None:
        if not obs:
            return None
        imbalance, spread = _num(obs.get("imbalance_top10")), _num(obs.get("spread_bps"))
        if imbalance is None or spread is None:
            return None
        oi_delta = 0.0
        if prior:
            old_oi, new_oi = _num(prior.get("oi_contracts")), _num(obs.get("oi_contracts"))
            if old_oi and new_oi is not None:
                oi_delta = new_oi / old_oi - 1.0
        return {
            **base, "imbalance_top10": imbalance,
            "book_delta_top10": _num(obs.get("book_delta_top10")) or 0.0,
            "book_delta_valid": 1.0 if obs.get("book_delta_valid") else 0.0,
            "spread_bps": spread, "oi_change_pct": oi_delta,
            "oi_change_valid": 1.0 if (prior and prior.get("oi_valid") and obs.get("oi_valid")) else 0.0,
        }

    micro_results = {}
    for target, family, horizons, rows, benchmark, prior_minutes in (
        ("scalping", "scalp_micro_5m", SCALP_HORIZONS, scalp, btc_by_ts, 5),
        ("reversals", "swing_micro_1H", SWING_HORIZONS, swing, btc1h_by_ts, 60),
    ):
        base = _features(rows, len(rows)-1, benchmark) if rows else None
        target_result = {"status": "IN APPRENDIMENTO", "by_direction": {}}
        obs = None
        prior = None
        if base and observations:
            close_ms = rows[-1]["_close_ms"]
            eligible = [o for o in observations
                        if _dt(o.get("captured_at")) is not None
                        and 0 <= close_ms - int(_dt(o["captured_at"]).timestamp() * 1000) <= 90000]
            obs = eligible[-1] if eligible else None
            if obs:
                obs_ms = int(_dt(obs["captured_at"]).timestamp() * 1000)
                target_ms = obs_ms - prior_minutes * 60000
                earlier = [o for o in observations
                           if _dt(o.get("captured_at")) is not None
                           and 0 <= target_ms - int(_dt(o["captured_at"]).timestamp() * 1000) <= 90000]
                prior = earlier[-1] if earlier else None
        if base and obs:
            raw = [_num(obs.get(k)) for k in (
                "cvd_1m_contracts", "cvd_5m_contracts", "cvd_15m_contracts",
                "imbalance_top10", "spread_bps")]
            if all(v is not None for v in raw):
                cvd1, cvd5, cvd15, imbalance, spread = raw
                signed_log = lambda value: math.copysign(math.log1p(abs(value)), value)
                oi_delta = 0.0
                if prior:
                    old_oi, new_oi = _num(prior.get("oi_contracts")), _num(obs.get("oi_contracts"))
                    if old_oi and new_oi is not None:
                        oi_delta = new_oi / old_oi - 1.0
                current_features = {
                    **base,
                    "cvd1m_log": signed_log(cvd1), "cvd5m_log": signed_log(cvd5),
                    "cvd15m_log": signed_log(cvd15), "imbalance_top10": imbalance,
                    "book_delta_top10": _num(obs.get("book_delta_top10")) or 0.0,
                    "book_delta_valid": 1.0 if obs.get("book_delta_valid") else 0.0,
                    "spread_bps": spread, "oi_change_pct": oi_delta,
                    "oi_change_valid": 1.0 if (prior and prior.get("oi_valid") and obs.get("oi_valid")) else 0.0,
                }
                for side in ("LONG", "SHORT"):
                    target_result["by_direction"][side] = {
                        str(h): _analogs(archive, family, side, h, current_features, MICRO_FEATURES, as_of_ms=rows[-1]["_close_ms"])
                        for h in horizons
                    }
                statuses = [item["status"] for side in target_result["by_direction"].values() for item in side.values()]
                target_result["status"] = "STIME SPERIMENTALI" if any(x == "STIMA SPERIMENTALE" for x in statuses) else "IN APPRENDIMENTO"
                target_result["current_features"] = current_features
            else:
                target_result["reason"] = "osservazione microstrutturale incompleta"
        else:
            target_result["reason"] = "osservazione microstrutturale non allineata alla candela completata"
        micro_results[target] = target_result
    # Queue execution needs current book/spread context, not CVD readiness.
    queue_features = None
    if scalp:
        current_bar = scalp[-1]
        current_base = _features(scalp, len(scalp)-1, btc_by_ts)
        if current_base:
            current_close_ms = current_bar["_close_ms"]
            captured_ms = int(captured.timestamp() * 1000) if captured else current_close_ms
            # The latest completed 5m candle can be up to 6m old while the
            # live book/context remains fresh; never use observations after snapshot capture.
            current_obs = None
            if observations:
                candidate = observations[-1]
                candidate_dt = _dt(candidate.get("captured_at"))
                if candidate_dt is not None:
                    candidate_ms = int(candidate_dt.timestamp() * 1000)
                    if 0 <= captured_ms - candidate_ms <= 180000:
                        current_obs = candidate
            if current_obs:
                current_obs_ms = int(_dt(current_obs["captured_at"]).timestamp() * 1000)
                current_prior = nearest_context(current_obs_ms - 5*60000)
                queue_features = enrich_queue(current_base, current_obs, current_prior)
    result["microstructure_models"] = micro_results
    queue_results = {"status": "IN APPRENDIMENTO", "by_direction": {}, "preferred_offsets": {}}
    current_scalp_features = queue_features
    if current_scalp_features:
        for side in ("LONG", "SHORT"):
            queue_results["by_direction"][side] = {}
            queue_results["preferred_offsets"][side] = {}
            for horizon in SCALP_HORIZONS:
                queue_results["by_direction"][side][str(horizon)] = {}
                candidates = []
                for offset in LIMIT_OFFSETS_ATR:
                    family = f"scalp_queue_{int(offset*100):02d}"
                    estimate = _analogs(archive, family, side, horizon,
                                        current_scalp_features, QUEUE_FEATURES, as_of_ms=scalp[-1]["_close_ms"])
                    if "p_limit_touch_proxy" in estimate:
                        estimate["p_queue_depletion_proxy"] = estimate.pop("p_limit_touch_proxy")
                    estimate["limit_offset_atr"] = offset
                    estimate["pending_filled_neighbors"] = max(
                        0, estimate.get("filled_proxy_neighbors", 0) - estimate.get("filled_neighbors", 0)
                    )
                    if (estimate.get("p_queue_depletion_proxy") is not None
                            and estimate.get("p_tp_before_sl_given_touch") is not None
                            and estimate["pending_filled_neighbors"] == 0):
                        estimate["p_joint_tp1_proxy"] = round(
                            estimate["p_queue_depletion_proxy"] * estimate["p_tp_before_sl_given_touch"], 4
                        )
                    else:
                        estimate["p_joint_tp1_proxy"] = None
                    estimate["execution_note"] = "Deplezione della coda visibile stimata da book sequenziato e trades aggressivi; non equivale a fill reale."
                    queue_results["by_direction"][side][str(horizon)][str(offset)] = estimate
                    if estimate.get("status") == "STIMA SPERIMENTALE" and estimate.get("p_joint_tp1_proxy") is not None:
                        candidates.append((estimate["p_joint_tp1_proxy"], offset))
                if candidates:
                    best_joint, best_offset = max(candidates)
                    queue_results["preferred_offsets"][side][str(horizon)] = {
                        "offset_atr": best_offset,
                        "joint_tp1_proxy": best_joint,
                        "basis": "confronto sperimentale fra offset; non calibrato",
                    }
        queue_statuses = [
            item["status"]
            for side in queue_results["by_direction"].values()
            for horizon in side.values()
            for item in horizon.values()
        ]
        queue_results["status"] = "STIME SPERIMENTALI" if any(x == "STIMA SPERIMENTALE" for x in queue_statuses) else "IN APPRENDIMENTO"
    try:
        queue_data = json.loads(QUEUE_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        queue_data = {}
    last_capture = queue_data.get("capture_status", [{}])[-1] if queue_data.get("capture_status") else {}
    ws_books = [row for row in queue_data.get("book_snapshots", []) if row.get("source") == "websocket"]
    ws_trades = [row for row in queue_data.get("trades", []) if row.get("source") == "websocket"]
    queue_results["data_coverage"] = {
        "book_snapshots": len(queue_data.get("book_snapshots", [])),
        "websocket_book_snapshots": len(ws_books),
        "websocket_trades": len(ws_trades),
        "capture_checks": len(queue_data.get("capture_status", [])),
        "last_capture_at": last_capture.get("captured_at"),
        "last_capture_book_valid": last_capture.get("book_valid"),
        "last_capture_trade_tape_truncated": last_capture.get("trade_events_truncated"),
        "last_capture_trade_age_seconds": last_capture.get("trade_age_seconds"),
        "last_capture_book_age_seconds": last_capture.get("book_age_seconds"),
        "last_capture_trade_gap_count": last_capture.get("trade_gap_count"),
        "last_capture_book_gap_count": last_capture.get("book_gap_count"),
        "oldest_ws_book_ts_ms": ws_books[0].get("ts_ms") if ws_books else None,
        "latest_ws_book_ts_ms": ws_books[-1].get("ts_ms") if ws_books else None,
    }
    queue_results["caveat"] = "Stima sperimentale: assume ordine in coda dopo la profondità visibile; cancellazioni, hidden liquidity, latenza e fill parziali non sono osservabili completamente."
    result["queue_execution_model"] = queue_results
    setup_rows = context_data.get("setups", [])
    setup_summary = {}
    for side in ("LONG", "SHORT"):
        setup_summary[side] = {}
        for horizon in SCALP_HORIZONS + SWING_HORIZONS:
            outcomes = [setup.get("outcomes", {}).get(str(horizon), {}).get("status")
                        for setup in setup_rows if setup.get("direction") == side]
            outcomes = [x for x in outcomes if x is not None]
            setup_summary[side][str(horizon)] = {
                "matured_setups": len(outcomes),
                "tp1": outcomes.count("TP1"),
                "sl": outcomes.count("SL") + outcomes.count("SL_AMBIGUO"),
                "none": outcomes.count("NONE"),
                "status": "IN APPRENDIMENTO" if len(outcomes) < MIN_ANALOGS else "CAMPIONE DA VALIDARE",
            }
    result["confirmed_trigger_validation"] = {
        "setups_total": len(setup_rows),
        "by_direction": setup_summary,
        "interpretation": "Esiti su trigger confermati, entry di riferimento alla chiusura candela; non sono fill LIMIT reali né win-rate indipendenti.",
    }
    movement_rows = context_data.get("movement_events", [])
    movement_summary = {}
    for side in ("LONG", "SHORT"):
        movement_summary[side] = {}
        for horizon in SCALP_HORIZONS + SWING_HORIZONS:
            values = [event.get("outcomes", {}).get(str(horizon), {}).get("status")
                      for event in movement_rows if event.get("direction") == side]
            values = [value for value in values if value is not None]
            movement_summary[side][str(horizon)] = {
                "matured_events": len(values),
                "tp1": values.count("TP1"),
                "sl": values.count("SL") + values.count("SL_AMBIGUO"),
                "none": values.count("NONE"),
            }
    result["movement_event_study"] = {
        "events_total": len(movement_rows),
        "outcomes_by_direction": movement_summary,
        "recent_events": movement_rows[-8:],
        "interpretation": "Eventi selezionati per movimenti anomali 5m/15m; fattori osservati prima della chiusura, non prova di causalità.",
    }
    result["context_archive"] = {
        "observations_total": len(observations),
        "last_observation_at": observations[-1].get("captured_at") if observations else None,
        "setups_total": len(setup_rows),
        "movement_events_total": len(movement_rows),
        "observation_gaps": sum(1 for a, b in zip(observations, observations[1:]) if int(b.get("minute_ms", 0)) - int(a.get("minute_ms", 0)) > 60000),
    }
    result["reversals"]["context_4h"] = four_hour_context
    result["reversals"]["context_4h_note"] = "Contesto descrittivo separato; non entra ancora nel calcolo delle frequenze analogiche."
    # Safety is explicit and deliberately duplicated at the engine boundary.
    result["safety"] = {
        **contract_safety,
        "orders_enabled": False,
        "position_sizing_enabled": False,
        "instrument_id_pinned": INSTRUMENT_ID,
    }
    return result
