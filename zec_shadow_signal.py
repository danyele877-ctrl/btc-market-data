#!/usr/bin/env python3
"""Independent, diagnostic-only directional shadow for the ZEC radar.

The baseline early-signal engine is never modified. This shadow recomputes a
separate directional score from price momentum, ZEC/BTC relative strength and
one correlated order-flow cluster, then applies mirrored support/resistance
confirmation gates. It cannot enable orders or sizing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from zec_early_signal import (
    FEED_KEY, _candlestick_direction, _clamp, _number, _price_change, _series,
    evaluate_signal,
)


def _shadow_series(snapshot: dict[str, Any], instrument: str, bar: str,
                   now: datetime | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Use baseline candle integrity checks, then apply SHADOW's own live-age gate."""
    # Preserve SHADOW's existing 360s 5m diagnostic freshness criterion.
    # BASELINE's stricter 180s blocker remains separate and SHADOW cannot
    # enable execution or sizing.
    rows, error = _series(snapshot, instrument, bar, max_age_seconds=360 if bar == "5m" else 180)
    if error:
        return [], error
    try:
        captured = datetime.fromisoformat(str(snapshot.get("source_captured_at")).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(rows[-1].get("completed_at")).replace("Z", "+00:00"))
        reference = now or captured
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=captured.tzinfo)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=captured.tzinfo)
        age = (reference - completed).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return [], f"candele {instrument} {bar}: timestamp SHADOW non valido"
    max_age = 180 if bar == "1m" else 360
    if age < -1 or age > max_age:
        return [], f"candele {instrument} {bar}: barra chiusa SHADOW stantia o fuori tempo ({age:.1f}s; limite {max_age}s)"
    return rows, None


def _structure(snapshot: dict[str, Any], x5m: list[dict[str, Any]],
               relative: float | None, flow: float | None) -> dict[str, Any]:
    if len(x5m) < 7:
        return {"status": "NON VERIFICABILE", "veto": "storico 5m insufficiente"}
    previous = x5m[-4:-1]
    highs = [_number(row.get("high")) for row in previous]
    lows = [_number(row.get("low")) for row in previous]
    ranges = [
        _number(row.get("high")) - _number(row.get("low"))
        for row in x5m[-6:]
        if _number(row.get("high")) is not None and _number(row.get("low")) is not None
    ]
    close = _number(x5m[-1].get("close"))
    volume_values = [_number(row.get("volume")) for row in x5m[-6:]]
    if (close is None or not highs or not lows or any(v is None for v in highs + lows)
            or len(ranges) != 6 or any(v is None or v < 0 for v in volume_values)):
        return {"status": "NON VERIFICABILE", "veto": "OHLC/volume 5m incompleto"}
    atr_proxy = sum(max(v, 0.0) for v in ranges) / len(ranges)
    if atr_proxy <= 0:
        return {"status": "NON VERIFICABILE", "veto": "ATR proxy 5m non valido"}
    support, resistance = min(lows), max(highs)
    baseline_volume = sum(volume_values[:-1]) / 5
    volume_ratio = volume_values[-1] / baseline_volume if baseline_volume > 0 else None
    volume_ok = volume_ratio is not None and volume_ratio >= 1.5
    rel_long = relative is not None and relative >= 0.0005
    rel_short = relative is not None and relative <= -0.0005
    flow_long = flow is not None and flow > 0.20
    flow_short = flow is not None and flow < -0.20
    breakout = close > resistance and volume_ok and (rel_long or flow_long)
    breakdown = close < support and volume_ok and (rel_short or flow_short)
    near_resistance = close >= resistance - 0.60 * atr_proxy
    near_support = close <= support + 0.60 * atr_proxy
    return {
        "status": "BREAKOUT RIALZISTA" if breakout else "BREAKDOWN RIBASSISTA" if breakdown else "NON CONFERMATO",
        "support": round(support, 8),
        "resistance": round(resistance, 8),
        "close_5m": round(close, 8),
        "atr_proxy_5m": round(atr_proxy, 8),
        "volume_ratio_5m": round(volume_ratio, 4) if volume_ratio is not None else None,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "breakout_confirmed": breakout,
        "breakdown_confirmed": breakdown,
        "veto": None,
    }


def _higher_timeframe_regime(snapshot: dict[str, Any], timeframe: str,
                             seconds: int, minimum_bars: int = 200,
                             history_key: str | None = None) -> dict[str, Any]:
    """Classify only a fully continuous, closed X-Perp history; otherwise N/D."""
    from datetime import timezone

    captured_raw = snapshot.get("source_captured_at")
    try:
        captured = datetime.fromisoformat(str(captured_raw).replace("Z", "+00:00"))
        captured = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
        captured = captured.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return {"status": "N/D", "reason": "source_captured_at non valido"}
    history = snapshot.get("predictive_history") or {}
    raw = history.get(history_key or f"xperp_{timeframe}")
    if not isinstance(raw, list):
        return {"status": "N/D", "reason": "serie storica non pubblicata"}
    rows = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(row["ts_ms"])
            completed = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            completed = completed.replace(tzinfo=timezone.utc) if completed.tzinfo is None else completed
            completed = completed.astimezone(timezone.utc)
            close = float(row["close"])
            if not (close > 0 and completed.timestamp() * 1000 == ts + seconds * 1000
                    and completed <= captured and ts % (seconds * 1000) == 0):
                continue
            rows.append((ts, completed, close))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    rows.sort(key=lambda item: item[0])
    if len(rows) < minimum_bars:
        return {"status": "N/D", "bars": len(rows), "reason": f"servono {minimum_bars} barre chiuse"}
    rows = rows[-minimum_bars:]
    if any(b[0] - a[0] != seconds * 1000 for a, b in zip(rows, rows[1:])):
        return {"status": "N/D", "bars": len(rows), "reason": "gap o duplicati nella serie"}
    age = (captured - rows[-1][1]).total_seconds()
    if age < 0 or age > 2 * seconds:
        return {"status": "N/D", "bars": len(rows), "age_seconds": round(age, 1),
                "reason": "ultima barra chiusa stale"}
    closes = [item[2] for item in rows]

    def ema(values: list[float], period: int) -> list[float]:
        alpha = 2.0 / (period + 1)
        result = [values[0]]
        for value in values[1:]:
            result.append(alpha * value + (1.0 - alpha) * result[-1])
        return result

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    last = closes[-1]
    if last > e20[-1] > e50[-1] > e200[-1] and e20[-1] > e20[-6]:
        regime = "TREND RIALZISTA"
    elif last < e20[-1] < e50[-1] < e200[-1] and e20[-1] < e20[-6]:
        regime = "TREND RIBASSISTA"
    elif abs(e20[-1] / e50[-1] - 1.0) <= 0.001:
        regime = "LATERALE / COMPRESSIONE"
    elif (last > e20[-1] and e50[-1] < e200[-1]) or (last < e20[-1] and e50[-1] > e200[-1]):
        regime = "POSSIBILE INVERSIONE"
    else:
        regime = "LATERALE / MISTO"
    return {
        "status": "OK", "regime": regime, "bars": len(rows),
        "last_closed_at": rows[-1][1].isoformat().replace("+00:00", "Z"),
        "age_seconds": round(age, 1), "close": round(last, 8),
        "ema20": round(e20[-1], 8), "ema50": round(e50[-1], 8),
        "ema200": round(e200[-1], 8),
    }


def _combined_regime(one_hour: dict[str, Any], four_hour: dict[str, Any]) -> dict[str, Any]:
    a, b = one_hour.get("regime"), four_hour.get("regime")
    if not a or not b:
        return {"status": "N/D", "regime": "REGIME TIMEFRAME SUPERIORE N/D",
                "reason": "servono 200 barre continue e fresche sia 1H sia 4H"}
    if a == b:
        return {"status": "OK", "regime": a, "alignment": "1H/4H concordi"}
    if a.startswith("TREND") and b.startswith("TREND"):
        return {"status": "MISTO", "regime": "CONFLITTO 1H/4H",
                "reason": "timeframe superiori discordanti"}
    return {"status": "MISTO", "regime": "TRANSIZIONE / LATERALE",
            "reason": "classificazioni 1H/4H non concordi"}


def evaluate_shadow_signal(snapshot: dict[str, Any], baseline: dict[str, Any] | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """Compute a separate directional diagnostic; baseline remains read-only."""
    baseline = baseline or evaluate_signal(snapshot, now=now)
    quality = baseline.get("data_quality") or {}
    # The shared BASELINE result applies its 180s nowcast gate. This is not a
    # SHADOW criterion: remove only that specific blocker, then apply SHADOW's
    # own unchanged 360s 5m freshness gate below. Other baseline safety/data
    # blockers continue to propagate.
    blockers = [
        item for item in (quality.get("blockers") or [])
        if not str(item).startswith("nowcast 5m bloccato:")
    ]
    x1m, x1m_error = _shadow_series(snapshot, FEED_KEY, "1m", now)
    x5m, x5m_error = _shadow_series(snapshot, FEED_KEY, "5m", now)
    btc1m, btc1m_error = _shadow_series(snapshot, "BTC-USDC", "1m", now)
    zec1m, zec1m_error = _shadow_series(snapshot, "ZEC-USDC", "1m", now)
    btc5m, btc_error = _shadow_series(snapshot, "BTC-USDC", "5m", now)
    zec5m, zec_error = _shadow_series(snapshot, "ZEC-USDC", "5m", now)
    for error in (x1m_error, x5m_error, btc1m_error, zec1m_error, btc_error, zec_error):
        if error and error not in blockers:
            blockers.append(error)
    for bar, streams in (("1m", (x1m, btc1m, zec1m)), ("5m", (x5m, btc5m, zec5m))):
        if all(streams) and len({rows[-1].get("ts_ms") for rows in streams}) != 1:
            blockers.append(f"coerenza temporale {bar} BTC/ZEC/X-Perp non valida per SHADOW")
    if btc5m and zec5m and btc5m[-1].get("ts_ms") == zec5m[-1].get("ts_ms"):
        # The baseline's relative-strength blocker can be a consequence of
        # its stricter 180s freshness gate. Re-evaluate alignment on SHADOW's
        # own validated series before deciding whether that blocker applies.
        blockers = [item for item in blockers if item != "forza relativa 5m ZEC/BTC non allineata o non disponibile"]
    elif btc5m and zec5m:
        blockers.append("coerenza temporale 5m BTC/ZEC non valida per SHADOW")

    d1 = _candlestick_direction(x1m) if x1m else 0
    d5 = _candlestick_direction(x5m) if x5m else 0
    price_score = (d1 + d5) / 2 if x1m and x5m else 0.0
    relative = None
    if btc5m and zec5m and btc5m[-1].get("ts_ms") == zec5m[-1].get("ts_ms"):
        zec_return, btc_return = _price_change(zec5m), _price_change(btc5m)
        if zec_return is not None and btc_return is not None:
            relative = zec_return - btc_return
    relative_score = _clamp(relative / 0.005) if relative is not None else None

    micro = snapshot.get("microstructure") or {}
    websocket = micro.get("websocket") or {}
    cvd = micro.get("cvd") or {}
    book = micro.get("book_imbalance") or {}
    ws_ready = quality.get("websocket_ready") is True
    flow_score = None
    flow_divergence = {
        "status": "N/D", "side": None, "score_adjustment": 0.0,
        "description": "CVD/prezzo non verificabili",
    }
    if ws_ready:
        cvd1, cvd5 = _number(cvd.get("1m")), _number(cvd.get("5m"))
        imbalance = _number(book.get("imbalance_top10"))
        price_return5 = _price_change(x5m) if x5m else None
        if cvd1 is not None and cvd5 is not None and imbalance is not None:
            cvd_score = (_sign(cvd1) + _sign(cvd5)) / 2
            book_score = _clamp(imbalance / 0.35)
            # CVD and book remain one correlated order-flow cluster. A small,
            # explicit price-response adjustment detects absorption without
            # treating raw contract counts as ZEC exposure or position size.
            adjustment = 0.0
            if price_return5 is not None and cvd5 < 0 and price_return5 > 0.0003:
                adjustment = 0.15
                flow_divergence = {
                    "status": "ASSORBIMENTO_VENDITE",
                    "side": "LONG",
                    "score_adjustment": adjustment,
                    "description": "CVD 5m negativo ma prezzo in rialzo: possibile assorbimento della pressione di vendita",
                }
            elif price_return5 is not None and cvd5 > 0 and price_return5 < -0.0003:
                adjustment = -0.15
                flow_divergence = {
                    "status": "ASSORBIMENTO_ACQUISTI",
                    "side": "SHORT",
                    "score_adjustment": adjustment,
                    "description": "CVD 5m positivo ma prezzo in ribasso: possibile assorbimento della pressione di acquisto",
                }
            else:
                flow_divergence = {
                    "status": "NESSUNA_DIVERGENZA_EVIDENTE",
                    "side": None,
                    "score_adjustment": 0.0,
                    "description": "CVD e risposta del prezzo non mostrano una divergenza significativa",
                }
            flow_score = _clamp(0.65 * cvd_score + 0.35 * book_score + adjustment)

    components = {"price": (price_score, 0.45)}
    if relative_score is not None:
        components["relative_strength"] = (relative_score, 0.30)
    if flow_score is not None:
        components["order_flow_cluster"] = (flow_score, 0.25)
    weight_sum = sum(weight for _, weight in components.values())
    score = sum(value * weight for value, weight in components.values()) / weight_sum if weight_sum else 0.0
    if score >= 0.10:
        candidate, basis = "LONG", "punteggio shadow positivo oltre la soglia neutrale"
    elif score <= -0.10:
        candidate, basis = "SHORT", "punteggio shadow negativo oltre la soglia neutrale"
    else:
        candidate, basis = None, "nessuna direzione: evidenze insufficienti o in conflitto"

    regime_1h = _higher_timeframe_regime(snapshot, "1H", 3600)
    regime_4h = _higher_timeframe_regime(snapshot, "4H", 14400)
    higher_timeframe_regime = _combined_regime(regime_1h, regime_4h)
    btc_1h = _higher_timeframe_regime(snapshot, "1H", 3600, history_key="btc_1H")
    btc_filter = {
        "status": "N/D",
        "regime_1H": btc_1h.get("regime"),
        "reason": (btc_1h.get("reason") if btc_1h.get("status") != "OK" else "regime BTC 1H verificato su 200 barre chiuse e continue"),
        "relative_strength_5m": relative,
        "veto": None,
    }
    if btc_1h.get("status") == "OK":
        if btc_1h.get("regime") == "TREND RIBASSISTA":
            btc_filter["status"] = "FILTRO BTC RIBASSISTA"
        elif btc_1h.get("regime") == "TREND RIALZISTA":
            btc_filter["status"] = "FILTRO BTC RIALZISTA"
        else:
            btc_filter["status"] = "FILTRO BTC NEUTRO / TRANSIZIONE"

    structure = _structure(snapshot, x5m, relative, flow_score)
    if structure.get("breakout_confirmed"):
        structure["setup_type"] = "BREAKOUT_LONG"
    elif structure.get("breakdown_confirmed"):
        structure["setup_type"] = "BREAKDOWN_SHORT"
    elif (candidate == "LONG" and regime_1h.get("regime") == "TREND RIALZISTA"
          and structure.get("near_support") and d1 >= 0
          and ((relative is not None and relative > 0) or (flow_score is not None and flow_score > 0.20))):
        structure["setup_type"] = "PULLBACK_LONG"
    elif (candidate == "SHORT" and regime_1h.get("regime") == "TREND RIBASSISTA"
          and structure.get("near_resistance") and d1 <= 0
          and ((relative is not None and relative < 0) or (flow_score is not None and flow_score < -0.20))):
        structure["setup_type"] = "PULLBACK_SHORT"
    elif (candidate == "LONG" and structure.get("near_support") and d1 > 0
          and flow_score is not None and flow_score > 0.20
          and (relative is None or relative >= 0)):
        structure["setup_type"] = "REVERSAL_LONG"
    elif (candidate == "SHORT" and structure.get("near_resistance") and d1 < 0
          and flow_score is not None and flow_score < -0.20
          and (relative is None or relative <= 0)):
        structure["setup_type"] = "REVERSAL_SHORT"
    else:
        structure["setup_type"] = "RANGE / NO SETUP"

    veto = None
    if candidate == "SHORT" and structure.get("near_support") and not structure.get("breakdown_confirmed"):
        veto = "VETO SHORT: vicino/sotto supporto senza breakdown 5m confermato; attendere chiusura e retest/conferma equivalente"
    elif candidate == "LONG" and structure.get("near_resistance") and not structure.get("breakout_confirmed"):
        veto = "VETO LONG: vicino/sopra resistenza senza breakout/recovery 5m confermato"
    # BTC 1H is a regime filter, not a standalone direction signal. Countertrend
    # ZEC setups need clear relative outperformance/underperformance or a confirmed break.
    if (candidate == "LONG" and btc_1h.get("regime") == "TREND RIBASSISTA"
            and not structure.get("breakout_confirmed")
            and (relative is None or relative < 0.0010)):
        veto = "VETO BTC: regime BTC 1H ribassista; LONG ZEC senza sovraperformance relativa sufficiente o breakout confermato"
        btc_filter["veto"] = veto
    elif (candidate == "SHORT" and btc_1h.get("regime") == "TREND RIALZISTA"
            and not structure.get("breakdown_confirmed")
            and (relative is None or relative > -0.0010)):
        veto = "VETO BTC: regime BTC 1H rialzista; SHORT ZEC senza sottoperformance relativa sufficiente o breakdown confermato"
        btc_filter["veto"] = veto
    structure["veto"] = veto

    aligned = bool(candidate is not None and d1 and d5 and d1 == d5 == (1 if candidate == "LONG" else -1))
    data_ready = not blockers and ws_ready and x1m and x5m and btc5m and zec5m
    confirmed = bool(data_ready and candidate is not None and not veto and abs(score) >= 0.72 and aligned)
    if not data_ready or candidate is None or veto:
        status = "ATTENDERE"
    elif confirmed:
        status = "TRIGGER CONFERMATO"
    elif abs(score) >= 0.45:
        status = "SETUP IN FORMAZIONE"
    else:
        status = "ATTENDERE"

    return {
        "schema_version": 1,
        "engine": "ZEC Directional Shadow",
        "mode": "diagnostic_only",
        "validation_status": "IN RACCOLTA — NESSUNA VALIDAZIONE",
        "baseline_unchanged": True,
        "instrument": "ZEC-USD_UM_XPERP-310530",
        "status": status,
        "decision": candidate if confirmed else "ATTENDERE",
        "candidate_direction": candidate,
        "candidate_direction_basis": basis,
        "score": round(score * 100, 1),
        "score_unit": "heuristic points, not probability",
        "components": {
            name: {"score": round(value, 4), "weight": weight}
            for name, (value, weight) in components.items()
        },
        "relative_strength_5m": relative,
        "flow_cluster": flow_score,
        "flow_divergence": flow_divergence,
        "btc_filter": btc_filter,
        "structure": structure,
        "higher_timeframe_regime": {
            "1H": regime_1h, "4H": regime_4h, "combined": higher_timeframe_regime,
        },
        "data_quality": {"ready": bool(data_ready), "websocket_ready": ws_ready, "blockers": blockers},
        "baseline_comparison": {
            "status": baseline.get("status"),
            "decision": baseline.get("decision"),
            "candidate_direction": baseline.get("candidate_direction"),
        },
        "orders_enabled": False,
        "position_sizing_enabled": False,
        "note": "Shadow diagnostico separato: non modifica né sostituisce BASELINE e non autorizza ordini.",
    }


def _sign(value: float | None) -> int:
    if value is None or abs(value) <= 1e-12:
        return 0
    return 1 if value > 0 else -1
