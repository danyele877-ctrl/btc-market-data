#!/usr/bin/env python3
"""Persistent, read-only ZEC microstructure and confirmed-trigger outcome archive."""
from __future__ import annotations

import json
import math
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


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") == 1 and value.get("instrument") == INSTRUMENT_ID:
                value.setdefault("observations", [])
                value.setdefault("setups", [])
                return value
        except Exception:
            pass
    return {
        "schema_version": 1,
        "instrument": INSTRUMENT_ID,
        "updated_at": None,
        "observations": [],
        "setups": [],
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
    if not all(cvd.get(f"ready_{window}") is True and _num(cvd.get(window)) is not None
               for window in ("1m", "5m", "15m")):
        return None
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
        "cvd_1m_contracts": _num(cvd.get("1m")),
        "cvd_5m_contracts": _num(cvd.get("5m")),
        "cvd_15m_contracts": _num(cvd.get("15m")),
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
        "trade_gap_count": int(ws.get("trade_gap_count") or 0),
        "book_gap_count": int(ws.get("book_gap_count") or 0),
        "trade_age_seconds": _num(ws.get("trade_age_seconds")),
        "book_age_seconds": _num(ws.get("book_age_seconds")),
        "unit": "contracts; ctVal unresolved; no conversion to ZEC",
    }


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
                "entry_type": "trigger_close_reference_not_actual_limit_fill",
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
    archive["setups"] = setups[-MAX_SETUPS:]
    archive["updated_at"] = captured.isoformat().replace("+00:00", "Z")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
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
        "trigger_validation": matured_counts,
        "note": "Trigger riferiti alla chiusura della candela confermata, non a un fill LIMIT reale; osservazioni microstrutturali in contratti, ctVal non risolto.",
    }
