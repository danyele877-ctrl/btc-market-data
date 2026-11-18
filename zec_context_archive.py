#!/usr/bin/env python3
"""Persistent, read-only ZEC microstructure and confirmed-trigger outcome archive."""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
ARCHIVE_PATH = Path("data/zec_context_archive.json")
HORIZONS = (5, 15, 30, 40, 60, 120, 240)
MAX_OBSERVATIONS = 43200
MAX_SETUPS = 2000


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
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


def _closed_1m(rows: Any, captured: datetime) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    valid = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _num(row.get("ts_ms"))
        completed = _time(row.get("completed_at"))
        values = {key: _num(row.get(key)) for key in ("open", "high", "low", "close")}
        if ts is None or completed is None or completed > captured:
            continue
        if int(completed.timestamp() * 1000) != int(ts) + 60000:
            continue
        if any(value is None for value in values.values()):
            continue
        if values["high"] < max(values["open"], values["close"], values["low"]):
            continue
        if values["low"] > min(values["open"], values["close"], values["high"]):
            continue
        valid.append({**row, **values, "ts_ms": int(ts), "_close_ms": int(completed.timestamp() * 1000)})
    valid.sort(key=lambda row: row["ts_ms"])
    dedup = {}
    for row in valid:
        dedup[row["ts_ms"]] = row
    ordered = [dedup[key] for key in sorted(dedup)]
    # Keep only the latest continuous segment; never bridge missing minutes.
    segment = []
    for row in ordered:
        if segment and row["ts_ms"] - segment[-1]["ts_ms"] != 60000:
            segment = []
        segment.append(row)
    return segment


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Preserve the last valid context archive if a write is interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") == 1 and value.get("instrument") == INSTRUMENT_ID:
                value.setdefault("observations", [])
                value.setdefault("setups", [])
                value.setdefault("movement_events", [])
                return value
        except Exception:
            pass
    return {
        "schema_version": 1,
        "instrument": INSTRUMENT_ID,
        "updated_at": None,
        "observations": [],
        "setups": [],
        "movement_events": [],
    }


def _micro_observation(snapshot: dict[str, Any], captured: datetime) -> dict[str, Any] | None:
    market = snapshot.get("market") or {}
    ticker = (market.get("zec_xperp_ticker") or {}).get("first") or {}
    if ticker.get("instId") != INSTRUMENT_ID:
        return None
    if _num((market.get("zec_xperp_ticker") or {}).get("exchange_age_seconds")) is None:
        return None
    if _num((market.get("zec_xperp_ticker") or {}).get("exchange_age_seconds")) > 180:
        return None
    micro = snapshot.get("microstructure") or {}
    ws = micro.get("websocket") or {}
    cvd = micro.get("cvd") or {}
    book = ws.get("book") or {}
    if ws.get("connected") is not True or ws.get("book_valid") is not True:
        return None
    for key, limit in (("message_age_seconds", 120), ("trade_age_seconds", 180), ("book_age_seconds", 180)):
        age = _num(ws.get(key))
        if age is None or age < 0 or age > limit:
            return None
    # Book/trade observations remain useful while longer CVD windows warm up.
    # Missing CVD horizons are preserved as null, never fabricated as zero.
    bid, ask = _num(book.get("best_bid")), _num(book.get("best_ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    book_history = [row for row in (ws.get("book_history") or [])
                    if isinstance(row, dict) and _num(row.get("imbalance_top10")) is not None]
    book_delta = None
    if len(book_history) >= 2:
        first, last = book_history[0], book_history[-1]
        elapsed = (_num(last.get("ts_ms")) or 0) - (_num(first.get("ts_ms")) or 0)
        if 1000 <= elapsed <= 300000:
            book_delta = _num(last.get("imbalance_top10")) - _num(first.get("imbalance_top10"))
    oi_market = market.get("zec_xperp_open_interest") or {}
    oi_record = oi_market.get("first") or {}
    oi_age = _num(oi_market.get("exchange_age_seconds"))
    oi_valid = (
        oi_market.get("http_status") == 200 and str(oi_market.get("api_code")) == "0"
        and oi_record.get("instId") == INSTRUMENT_ID
        and oi_age is not None and 0 <= oi_age <= 180
    )
    oi = _num(oi_record.get("oi")) if oi_valid else None
    oi_price = _num(oi_record.get("oiCcy")) if oi_valid else None
    price = _num(ticker.get("last"))
    if price is None or price <= 0:
        return None
    return {
        "minute_ms": int(captured.timestamp() // 60 * 60000),
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "price": price,
        "cvd_1m_contracts": _num(cvd.get("1m")) if cvd.get("ready_1m") else None,
        "cvd_5m_contracts": _num(cvd.get("5m")) if cvd.get("ready_5m") else None,
        "cvd_15m_contracts": _num(cvd.get("15m")) if cvd.get("ready_15m") else None,
        "cvd_ready_1m": bool(cvd.get("ready_1m")),
        "cvd_ready_5m": bool(cvd.get("ready_5m")),
        "cvd_ready_15m": bool(cvd.get("ready_15m")),
        "imbalance_top5": _num(book.get("imbalance_top5")),
        "imbalance_top10": _num(book.get("imbalance_top10")),
        "imbalance_top20": _num(book.get("imbalance_top20")),
        "book_delta_top10": book_delta,
        "book_delta_valid": book_delta is not None,
        "spread_bps": ((ask - bid) / ((ask + bid) / 2)) * 10000,
        "best_bid": bid,
        "best_ask": ask,
        "oi_contracts": oi,
        "oi_reported_value": oi_price,
        "oi_valid": oi_valid,
        "funding_rate": _num(((market.get("zec_xperp_funding") or {}).get("first") or {}).get("fundingRate")),
        "funding_premium": _num(((market.get("zec_xperp_funding") or {}).get("first") or {}).get("premium")),
        "trade_gap_count": int(ws.get("trade_gap_count") or 0),
        "book_gap_count": int(ws.get("book_gap_count") or 0),
        "trade_age_seconds": _num(ws.get("trade_age_seconds")),
        "book_age_seconds": _num(ws.get("book_age_seconds")),
        "unit": "contracts; current EEA CTVAL source is 0.01 ZEC × ctMult=1; raw CVD/OI remain in contracts and do not enable sizing",
    }


def _movement_candidates(snapshot: dict[str, Any], observations: list[dict[str, Any]],
                         captured: datetime) -> list[dict[str, Any]]:
    """Detect unusually large completed 5m/15m moves and attach only prior context."""
    hist = snapshot.get("predictive_history") or {}
    rows = hist.get("xperp_1m")
    if not rows:
        rows = (((snapshot.get("candles_1m_5m") or {}).get("ZEC-USD_UM_XPERP") or {}).get("1m") or {}).get("tail", [])
    bars = _closed_1m(rows, captured)
    btc_rows = hist.get("btc_1m") or []
    btc_clean = []
    for row in btc_rows:
        if not isinstance(row, dict):
            continue
        ts = _num(row.get("ts_ms"))
        done = _time(row.get("completed_at"))
        close = _num(row.get("close"))
        if ts is not None and done is not None and done <= captured and int(done.timestamp()*1000) == int(ts)+60000 and close and close > 0:
            btc_clean.append({"ts_ms": int(ts), "_close_ms": int(done.timestamp()*1000), "close": close})
    btc_clean.sort(key=lambda row: row["_close_ms"])
    def btc_close_at(close_ms: int) -> float | None:
        eligible = [row for row in btc_clean if 0 <= close_ms - row["_close_ms"] <= 90000]
        return eligible[-1]["close"] if eligible else None
    ordered_obs = sorted(observations, key=lambda item: item.get("minute_ms", 0))
    events = []
    for i in range(20, len(bars)):
        bar = bars[i]
        close = bar["close"]
        atr_values = []
        for j in range(max(1, i-14), i+1):
            prev = bars[j-1]["close"]
            atr_values.append(max(bars[j]["high"]-bars[j]["low"],
                                  abs(bars[j]["high"]-prev), abs(bars[j]["low"]-prev)))
        atr = sum(atr_values)/len(atr_values) if atr_values else 0.0
        if close <= 0 or atr <= 0:
            continue
        ret5 = close / bars[i-5]["close"] - 1.0
        ret15 = close / bars[i-15]["close"] - 1.0
        threshold5 = max(0.004, 1.5 * atr / close * math.sqrt(5))
        threshold15 = max(0.0075, 2.0 * atr / close * math.sqrt(15))
        candidates = []
        if abs(ret5) >= threshold5:
            candidates.append((abs(ret5)/threshold5, 5, ret5))
        if abs(ret15) >= threshold15:
            candidates.append((abs(ret15)/threshold15, 15, ret15))
        if not candidates:
            continue
        _strength, horizon, move = max(candidates)
        direction = "LONG" if move > 0 else "SHORT"
        close_ms = bar["_close_ms"]
        prior_obs = [o for o in ordered_obs
                     if _time(o.get("captured_at")) is not None
                     and 0 <= close_ms - int(_time(o["captured_at"]).timestamp()*1000) <= 90000]
        obs = prior_obs[-1] if prior_obs else None
        if not obs:
            continue
        prior_target = close_ms - 5*60000
        earlier = [o for o in ordered_obs
                   if _time(o.get("captured_at")) is not None
                   and 0 <= prior_target - int(_time(o["captured_at"]).timestamp()*1000) <= 90000]
        prior = earlier[-1] if earlier else None
        oi_change = None
        if prior and prior.get("oi_valid") and obs.get("oi_valid"):
            old_oi, new_oi = _num(prior.get("oi_contracts")), _num(obs.get("oi_contracts"))
            if old_oi and new_oi is not None:
                oi_change = new_oi/old_oi - 1.0
        btc_return = None
        btc_now = btc_close_at(bar["_close_ms"])
        btc_prior = btc_close_at(bars[i-5]["_close_ms"])
        if btc_now is not None and btc_prior is not None and btc_prior > 0:
            btc_return = btc_now / btc_prior - 1.0
        relative = ret5 - btc_return if btc_return is not None else None
        cvd = {key: _num(obs.get(f"cvd_{key}_contracts")) for key in ("1m", "5m", "15m")}
        imbalance = _num(obs.get("imbalance_top10"))
        flow_alignment = "non disponibile"
        if cvd["5m"] is not None:
            flow_alignment = "concorde" if (cvd["5m"] > 0) == (direction == "LONG") and cvd["5m"] != 0 else "contrario" if cvd["5m"] != 0 else "neutro"
        book_alignment = "non disponibile"
        if imbalance is not None:
            book_alignment = "concorde" if (imbalance > 0) == (direction == "LONG") and imbalance != 0 else "contrario" if imbalance != 0 else "neutro"
        event_id = f"{INSTRUMENT_ID}:{close_ms}:{direction}:{horizon}"
        events.append({
            "id": event_id, "instrument": INSTRUMENT_ID,
            "captured_at": captured.isoformat().replace("+00:00", "Z"),
            "event_candle_completed_at": datetime.fromtimestamp(close_ms/1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "context_observation_at": obs.get("captured_at"),
            "entry_close_ms": close_ms, "direction": direction,
            "entry_type": "movement_close_reference_not_limit_fill",
            "move_horizon_min": horizon, "move_return_pct": round(move*100, 4),
            "entry_reference": close, "atr_reference": atr,
            "features": {
                "return_1m": round((close/bars[i-1]["close"]-1)*100, 4),
                "return_5m": round(ret5*100, 4), "return_15m": round(ret15*100, 4),
                "atr_pct": round(atr/close*100, 4),
                "btc_return_5m_pct": round(btc_return*100, 4) if btc_return is not None else None,
                "zec_relative_btc_5m_pct": round(relative*100, 4) if relative is not None else None,
                "cvd_contracts": cvd, "cvd_alignment": flow_alignment,
                "book_imbalance_top10": imbalance, "book_alignment": book_alignment,
                "book_delta_top10": _num(obs.get("book_delta_top10")),
                "spread_bps": _num(obs.get("spread_bps")),
                "oi_change_5m_pct": round(oi_change*100, 5) if oi_change is not None else None,
                "funding_rate": _num(obs.get("funding_rate")),
            },
            "observed_factors": [
                f"CVD 5m {flow_alignment}", f"book top10 {book_alignment}",
                "BTC 5m disponibile" if btc_return is not None else "BTC 5m non allineato",
                "OI 5m disponibile" if oi_change is not None else "OI 5m non disponibile",
            ],
            "causality_note": "Fattori temporalmente associati; non dimostrano causalità.",
            "outcomes": {},
        })
    return events


def _dedupe_movement_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive bars from the same displacement into one event episode."""
    kept = []
    for event in sorted(events, key=lambda item: item.get("entry_close_ms", 0)):
        if not kept:
            kept.append(event)
            continue
        previous = kept[-1]
        elapsed = (_num(event.get("entry_close_ms")) or 0) - (_num(previous.get("entry_close_ms")) or 0)
        if event.get("direction") == previous.get("direction") and elapsed < 15 * 60000:
            continue
        if event.get("direction") != previous.get("direction") and elapsed < 5 * 60000:
            continue
        kept.append(event)
    return kept


def _mature_setups(setups: list[dict[str, Any]], bars: list[dict[str, Any]]) -> int:
    updated = 0
    if not bars:
        return updated
    for setup in setups:
        if not isinstance(setup, dict):
            continue
        entry_time = _num(setup.get("entry_close_ms"))
        entry = _num(setup.get("entry_reference"))
        atr = _num(setup.get("atr_reference"))
        side = setup.get("direction")
        if entry_time is None or entry is None or atr is None or atr <= 0 or side not in ("LONG", "SHORT"):
            continue
        stop = entry - atr if side == "LONG" else entry + atr
        target = entry + 1.5 * atr if side == "LONG" else entry - 1.5 * atr
        outcomes = setup.setdefault("outcomes", {})
        for horizon in HORIZONS:
            key = str(horizon)
            if key in outcomes:
                continue
            deadline = entry_time + horizon * 60000
            future = [bar for bar in bars if bar["_close_ms"] > entry_time and bar["_close_ms"] <= deadline]
            # Require the full forward path from the first minute after entry;
            # a truncated 8-candle tail must never be mistaken for a complete horizon.
            if not future or future[0]["_close_ms"] > entry_time + 60000 or future[-1]["_close_ms"] < deadline:
                continue
            mfe = 0.0
            mae = 0.0
            result = "NONE"
            hit_time = None
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
                    result, hit_time = "SL_AMBIGUO", bar["_close_ms"]
                    break
                if stop_hit:
                    result, hit_time = "SL", bar["_close_ms"]
                    break
                if target_hit:
                    result, hit_time = "TP1", bar["_close_ms"]
                    break
            outcomes[key] = {
                "status": result,
                "mfe_atr": round(mfe, 4),
                "mae_atr": round(mae, 4),
                "hit_at_ms": hit_time,
                "matured_at_ms": future[-1]["_close_ms"],
                "entry_type": setup.get("entry_type") or ("movement_close_reference_not_limit_fill" if "move_horizon_min" in setup else "trigger_close_reference_not_actual_limit_fill"),
            }
            updated += 1
    return updated


def _record_confirmed_setup(snapshot: dict[str, Any], setups: list[dict[str, Any]],
                            captured: datetime, bars: list[dict[str, Any]]) -> int:
    signal = snapshot.get("zec_early_signal") or {}
    if signal.get("status") != "TRIGGER CONFERMATO" or signal.get("decision") not in ("LONG", "SHORT"):
        return 0
    if not bars:
        return 0
    latest = bars[-1]
    direction = signal["decision"]
    # One event per directional episode: suppress repeated minute-by-minute
    # confirmations for 40 minutes; an opposite trigger needs at least 5 minutes.
    previous = setups[-1] if setups else None
    if previous:
        previous_time = _num(previous.get("entry_close_ms")) or 0
        elapsed = latest["_close_ms"] - previous_time
        if previous.get("direction") == direction and elapsed < 40 * 60000:
            return 0
        if previous.get("direction") != direction and elapsed < 5 * 60000:
            return 0
    event_id = f"{INSTRUMENT_ID}:{latest['_close_ms']}:{direction}"
    if any(item.get("id") == event_id for item in setups):
        return 0
    atr = _num((((snapshot.get("zec_xperp_history") or {}).get("1m") or {}).get("indicators") or {}).get("atr14"))
    entry = _num(latest.get("close"))
    if atr is None or atr <= 0 or entry is None:
        return 0
    ws = (snapshot.get("microstructure") or {}).get("websocket") or {}
    cvd = (snapshot.get("microstructure") or {}).get("cvd") or {}
    book = ws.get("book") or {}
    groups = ((signal.get("scores") or {}).get("groups") or {})
    setups.append({
        "id": event_id,
        "instrument": INSTRUMENT_ID,
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "entry_close_ms": latest["_close_ms"],
        "direction": direction,
        "entry_reference": entry,
        "atr_reference": atr,
        "structural_invalidation": (signal.get("invalidation") or {}).get("price"),
        "early_signal_score": (signal.get("scores") or {}).get("net"),
        "confidence_score": (signal.get("confidence") or {}).get("score"),
        "groups": {key: value.get("score") for key, value in groups.items()},
        "cvd_contracts": {key: _num(cvd.get(key)) for key in ("1m", "5m", "15m")},
        "book_imbalance": {key: _num(book.get(key)) for key in ("imbalance_top5", "imbalance_top10", "imbalance_top20")},
        "entry_type": "trigger_close_reference_not_actual_limit_fill",
        "outcomes": {},
    })
    return 1


def backfill_context_archive(rows: list[tuple[datetime, dict[str, Any]]],
                            archive_path: Path = ARCHIVE_PATH) -> dict[str, Any]:
    """Backfill many historical snapshots with one archive read/write cycle."""
    archive = _load(archive_path)
    observations = archive["observations"]
    setups = archive["setups"]
    by_minute = {int(item.get("minute_ms", 0)): index for index, item in enumerate(observations)}
    added_setups = 0
    matured = 0
    movement_events = _dedupe_movement_events(archive.setdefault("movement_events", []))
    movement_by_id = {item.get("id"): item for item in movement_events if isinstance(item, dict)}
    movement_ids = set(movement_by_id)
    movement_added = 0
    movement_matured = 0
    for captured, snapshot in sorted(rows, key=lambda item: item[0]):
        ticker = ((snapshot.get("market") or {}).get("zec_xperp_ticker") or {}).get("first") or {}
        if (snapshot.get("schema_version") != 3 or snapshot.get("source_status") != "ok"
                or ticker.get("instId") != INSTRUMENT_ID):
            continue
        observation = _micro_observation(snapshot, captured)
        if observation:
            minute = observation["minute_ms"]
            if minute in by_minute:
                index = by_minute[minute]
                if _time(observation["captured_at"]) >= _time(observations[index].get("captured_at")):
                    observations[index] = observation
            else:
                by_minute[minute] = len(observations)
                observations.append(observation)
        hist_rows = (snapshot.get("predictive_history") or {}).get("xperp_1m")
        if not hist_rows:
            hist_rows = (((snapshot.get("candles_1m_5m") or {}).get("ZEC-USD_UM_XPERP") or {}).get("1m") or {}).get("tail", [])
        bars = _closed_1m(hist_rows, captured)
        added_setups += _record_confirmed_setup(snapshot, setups, captured, bars)
        matured += _mature_setups(setups, bars)
        for event in _movement_candidates(snapshot, observations, captured):
            last_event = movement_events[-1] if movement_events else None
            elapsed = (event["entry_close_ms"] - last_event["entry_close_ms"]) if last_event else None
            cooldown = 15*60000 if last_event and last_event.get("direction") == event.get("direction") else 5*60000
            if event["id"] in movement_by_id:
                existing_event = movement_by_id[event["id"]]
                if not existing_event.get("event_candle_completed_at"):
                    existing_event["event_candle_completed_at"] = event.get("event_candle_completed_at")
                if not existing_event.get("context_observation_at"):
                    existing_event["context_observation_at"] = event.get("context_observation_at")
                existing_event["entry_type"] = event.get("entry_type", existing_event.get("entry_type"))
                for outcome in (existing_event.get("outcomes") or {}).values():
                    outcome["entry_type"] = existing_event.get("entry_type", "movement_close_reference_not_limit_fill")
                existing_features = existing_event.setdefault("features", {})
                for feature, value in (event.get("features") or {}).items():
                    if existing_features.get(feature) is None and value is not None:
                        existing_features[feature] = value
                if event.get("features", {}).get("btc_return_5m_pct") is not None:
                    existing_event["observed_factors"] = event.get("observed_factors", existing_event.get("observed_factors", []))
                continue
            if elapsed is None or elapsed >= cooldown:
                movement_ids.add(event["id"])
                movement_events.append(event)
                movement_by_id[event["id"]] = event
                movement_added += 1
        movement_matured += _mature_setups(movement_events, bars)
    observations.sort(key=lambda item: item.get("minute_ms", 0))
    archive["observations"] = observations[-MAX_OBSERVATIONS:]
    archive["setups"] = setups[-MAX_SETUPS:]
    archive["movement_events"] = sorted(movement_events, key=lambda item: item.get("entry_close_ms", 0))[-3000:]
    if rows:
        archive["updated_at"] = max(item[0] for item in rows).isoformat().replace("+00:00", "Z")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(archive_path, archive)
    gaps = sum(1 for a, b in zip(archive["observations"], archive["observations"][1:])
               if b["minute_ms"] - a["minute_ms"] > 60000)
    return {
        "status": "OK",
        "observations_total": len(archive["observations"]),
        "setups_total": len(archive["setups"]),
        "setups_added": added_setups,
        "outcomes_matured": matured,
        "movement_events_total": len(archive["movement_events"]),
        "movement_events_added": movement_added,
        "movement_outcomes_matured": movement_matured,
        "observation_gaps": gaps,
        "last_observation_at": archive["observations"][-1].get("captured_at") if archive["observations"] else None,
    }


def update_context_archive(snapshot: dict[str, Any], archive_path: Path = ARCHIVE_PATH,
                           now: datetime | None = None) -> dict[str, Any]:
    """Append one quality-gated microstructure observation and mature confirmed-trigger outcomes."""
    captured = _time(snapshot.get("source_captured_at"))
    if captured is None:
        return {"status": "BLOCCATO", "reason": "source_captured_at non valido", "observations_total": 0, "observations_added": 0, "setups_total": 0, "setups_added": 0, "outcomes_matured": 0, "trigger_validation": {}}
    current = now or datetime.now(timezone.utc)
    age = (current - captured).total_seconds()
    ticker = ((snapshot.get("market") or {}).get("zec_xperp_ticker") or {}).get("first") or {}
    if (snapshot.get("schema_version") != 3 or snapshot.get("source_status") != "ok"
            or ticker.get("instId") != INSTRUMENT_ID or age < -60 or age > 180):
        return {"status": "BLOCCATO", "reason": "snapshot non valido/stale o strumento errato", "observations_total": 0, "observations_added": 0, "setups_total": 0, "setups_added": 0, "outcomes_matured": 0, "trigger_validation": {}}
    archive = _load(archive_path)
    before_obs = len(archive["observations"])
    before_setups = len(archive["setups"])
    movement_events = _dedupe_movement_events(archive.setdefault("movement_events", []))
    movement_by_id = {item.get("id"): item for item in movement_events if isinstance(item, dict)}
    movement_ids = set(movement_by_id)
    movement_added = 0
    movement_matured = 0
    observation = _micro_observation(snapshot, captured)
    if observation:
        observations = archive["observations"]
        minute = observation["minute_ms"]
        existing = next((i for i, item in enumerate(observations) if item.get("minute_ms") == minute), None)
        if existing is None:
            observations.append(observation)
        elif _time(observation["captured_at"]) >= _time(observations[existing].get("captured_at")):
            observations[existing] = observation
        observations.sort(key=lambda item: item.get("minute_ms", 0))
        archive["observations"] = observations[-MAX_OBSERVATIONS:]
    hist_rows = (snapshot.get("predictive_history") or {}).get("xperp_1m")
    if not hist_rows:
        hist_rows = (((snapshot.get("candles_1m_5m") or {}).get("ZEC-USD_UM_XPERP") or {}).get("1m") or {}).get("tail", [])
    bars = _closed_1m(hist_rows, captured)
    setups = archive["setups"]
    added_setup = _record_confirmed_setup(snapshot, setups, captured, bars)
    matured = _mature_setups(setups, bars)
    for event in _movement_candidates(snapshot, archive["observations"], captured):
        last_event = movement_events[-1] if movement_events else None
        elapsed = (event["entry_close_ms"] - last_event["entry_close_ms"]) if last_event else None
        cooldown = 15*60000 if last_event and last_event.get("direction") == event.get("direction") else 5*60000
        if event["id"] in movement_by_id:
            existing_event = movement_by_id[event["id"]]
            if not existing_event.get("event_candle_completed_at"):
                existing_event["event_candle_completed_at"] = event.get("event_candle_completed_at")
            if not existing_event.get("context_observation_at"):
                existing_event["context_observation_at"] = event.get("context_observation_at")
            existing_event["entry_type"] = event.get("entry_type", existing_event.get("entry_type"))
            for outcome in (existing_event.get("outcomes") or {}).values():
                outcome["entry_type"] = existing_event.get("entry_type", "movement_close_reference_not_limit_fill")
            existing_features = existing_event.setdefault("features", {})
            for feature, value in (event.get("features") or {}).items():
                if existing_features.get(feature) is None and value is not None:
                    existing_features[feature] = value
            if event.get("features", {}).get("btc_return_5m_pct") is not None:
                existing_event["observed_factors"] = event.get("observed_factors", existing_event.get("observed_factors", []))
            continue
        if elapsed is None or elapsed >= cooldown:
            movement_ids.add(event["id"])
            movement_events.append(event)
            movement_by_id[event["id"]] = event
            movement_added += 1
    movement_matured += _mature_setups(movement_events, bars)
    archive["setups"] = setups[-MAX_SETUPS:]
    archive["movement_events"] = sorted(movement_events, key=lambda item: item.get("entry_close_ms", 0))[-3000:]
    archive["updated_at"] = captured.isoformat().replace("+00:00", "Z")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(archive_path, archive)
    obs = archive["observations"]
    gaps = sum(1 for a, b in zip(obs, obs[1:]) if b["minute_ms"] - a["minute_ms"] > 60000)
    matured_counts = {}
    for side in ("LONG", "SHORT"):
        matured_counts[side] = {}
        for horizon in HORIZONS:
            values = [setup.get("outcomes", {}).get(str(horizon), {}).get("status")
                      for setup in setups if setup.get("direction") == side]
            values = [value for value in values if value is not None]
            matured_counts[side][str(horizon)] = {
                "matured_setups": len(values),
                "tp1": values.count("TP1"),
                "sl": values.count("SL") + values.count("SL_AMBIGUO"),
                "none": values.count("NONE"),
            }
    return {
        "status": "OK" if observation else "MICROSTRUTTURA NON ARCHIVIATA",
        "observations_total": len(obs),
        "observations_added": max(0, len(obs) - before_obs),
        "last_observation_at": obs[-1].get("captured_at") if obs else None,
        "observation_gaps": gaps,
        "setups_total": len(setups),
        "setups_added": added_setup,
        "outcomes_matured": matured,
        "movement_events_total": len(archive["movement_events"]),
        "movement_events_added": movement_added,
        "movement_outcomes_matured": movement_matured,
        "trigger_validation": matured_counts,
        "note": "Trigger riferiti alla chiusura della candela confermata, non a un fill LIMIT reale; CVD/OI restano in contratti grezzi, la base CTVAL EEA verificata a 0.01 ZEC × ctMult=1 resta diagnostica e non abilita sizing o ordini.",
    }
