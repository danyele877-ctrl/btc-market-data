#!/usr/bin/env python3
"""Explainable, simulation-only ZEC early-signal engine for OKX EEA data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from zec_contract_config import build_contract_safety


INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
FEED_KEY = "ZEC-USD_UM_XPERP"
WEIGHTS = {
    "order_flow": 0.35,
    "positioning": 0.20,
    "relative_strength": 0.20,
    "microstructure": 0.25,
}
BAR_SECONDS = {"1m": 60, "5m": 300}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
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


def _sign(value: float | None, epsilon: float = 1e-12) -> int:
    if value is None or abs(value) <= epsilon:
        return 0
    return 1 if value > 0 else -1


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - 1.0


def _series(snapshot: dict[str, Any], instrument: str, bar: str,
            max_age_seconds: int | None = None, reference: datetime | None = None) -> tuple[list[dict[str, Any]], str | None]:
    rows = (((snapshot.get("candles_1m_5m") or {}).get(instrument) or {}).get(bar) or {}).get("tail")
    if not isinstance(rows, list):
        series = (((snapshot.get("candles_1m_5m") or {}).get(instrument) or {}).get(bar) or {})
        rows = [series.get("previous"), series.get("latest")]
    rows = [row for row in rows if isinstance(row, dict)]
    if len(rows) < 2:
        return [], f"candele {instrument} {bar}: meno di due barre chiuse"

    interval_ms = BAR_SECONDS[bar] * 1000
    starts: list[int] = []
    captured_at = _time(snapshot.get("source_captured_at"))
    for row in rows:
        try:
            ts_ms = int(row["ts_ms"])
        except (KeyError, TypeError, ValueError):
            return [], f"candele {instrument} {bar}: timestamp mancante"
        completed_at = _time(row.get("completed_at"))
        if completed_at is None or captured_at is None or completed_at > captured_at:
            return [], f"candele {instrument} {bar}: barra non completata entro l'acquisizione"
        if int(completed_at.timestamp() * 1000) != ts_ms + interval_ms:
            return [], f"candele {instrument} {bar}: timestamp di completamento incoerente"
        starts.append(ts_ms)
    if starts != sorted(starts) or len(starts) != len(set(starts)):
        return [], f"candele {instrument} {bar}: ordine o duplicati non validi"
    if any(b - a != interval_ms for a, b in zip(starts, starts[1:])):
        return [], f"candele {instrument} {bar}: intervallo mancante"

    latest_completed = _time(rows[-1].get("completed_at"))
    age = ((reference or captured_at) - latest_completed).total_seconds() if captured_at and latest_completed else None
    # A nowcast may only use a recent completed candle. The same 180 second
    # ceiling applies to 5m as well as 1m; a longer lookback is history, not
    # verified current-state evidence.
    max_age = 180 if max_age_seconds is None else max_age_seconds
    if age is None or age < -1 or age > max_age:
        if bar == "5m" and max_age == 180 and age is not None and age > max_age:
            return [], f"nowcast 5m bloccato: barra chiusa oltre 180 secondi ({age:.1f}s)"
        return [], f"candele {instrument} {bar}: barra chiusa stantia o fuori tempo ({age})"
    for row in rows:
        if any(_number(row.get(field)) is None for field in ("open", "high", "low", "close")):
            return [], f"candele {instrument} {bar}: OHLC non valido"
    return rows, None


def _aligned_pair(*series: list[dict[str, Any]]) -> bool:
    return all(len(rows) >= 2 for rows in series) and len({
        tuple(int(row["ts_ms"]) for row in rows[-2:]) for rows in series
    }) == 1


def _price_change(candles: list[dict[str, Any]]) -> float | None:
    if len(candles) < 2:
        return None
    return _pct_change(_number(candles[-1].get("close")), _number(candles[-2].get("close")))


def _candlestick_direction(candles: list[dict[str, Any]]) -> int:
    return _sign(_price_change(candles), epsilon=0.00005)


def _open_interest_signal(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    micro = snapshot.get("microstructure") or {}
    history = micro.get("open_interest_history") or micro.get("oi_snapshots") or []
    if not isinstance(history, list) or len(history) < 2:
        return None, "open interest: storico non ancora disponibile"
    points = []
    captured_at = _time(snapshot.get("source_captured_at"))
    for item in history:
        if not isinstance(item, dict):
            continue
        ts = _number(item.get("ts_ms"))
        oi = _number(item.get("oi_contracts", item.get("oi")))
        px = _number(item.get("price_usd", item.get("price")))
        if ts is not None and oi is not None and px is not None and oi > 0 and px > 0:
            points.append((int(ts), oi, px))
    points.sort(key=lambda item: item[0])
    if len(points) < 2:
        return None, "open interest: due campioni validi non disponibili"
    prior = next((point for point in reversed(points[:-1]) if points[-1][0] - point[0] >= 60_000), None)
    if prior is None:
        return None, "open interest: intervallo tra campioni insufficiente"
    current = points[-1]
    age = (captured_at.timestamp() * 1000 - current[0]) / 1000 if captured_at else None
    if age is None or age < -60 or age > 20 * 60:
        return None, "open interest: ultimo campione oltre 20 minuti o timestamp non valido"
    elapsed = (current[0] - prior[0]) / 1000
    if elapsed > 20 * 60:
        return None, "open interest: storico oltre 20 minuti"
    oi_change = _pct_change(current[1], prior[1])
    px_change = _pct_change(current[2], prior[2])
    if oi_change is None or px_change is None:
        return None, "open interest: variazione non calcolabile"

    # OI is contextualized with its paired price change and stays in its own
    # correlated cluster; it is never converted to ZEC, USD exposure, or size.
    if oi_change > 0.003 and px_change > 0.0005:
        score, explanation = 0.75, "OI e prezzo aumentano: nuove posizioni accompagnano il rialzo"
    elif oi_change > 0.003 and px_change < -0.0005:
        score, explanation = -0.75, "OI aumenta mentre il prezzo scende: nuove posizioni accompagnano la debolezza"
    elif oi_change < -0.003:
        score, explanation = 0.0, "OI in calo: chiusure/posizionamento in riduzione, direzione non inferita"
    else:
        score, explanation = 0.0, "variazione OI contenuta; nessuna direzione indipendente"
    return {
        "score": score,
        "oi_change_pct": round(oi_change * 100, 4),
        "price_change_pct": round(px_change * 100, 4),
        "elapsed_seconds": round(elapsed, 1),
        "unit": "contracts",
        "interpretation": explanation,
    }, None


def _funding_interval_diagnostic(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read the live OKX interval from fundingTime -> nextFundingTime."""
    record = (snapshot.get("market") or {}).get("zec_xperp_funding") or {}
    funding = record.get("first") or {}

    def epoch_ms(value: Any) -> int | None:
        try:
            number = int(value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    previous = epoch_ms(funding.get("prevFundingTime"))
    current = epoch_ms(funding.get("fundingTime"))
    following = epoch_ms(funding.get("nextFundingTime"))
    observed = {}
    if previous is not None and current is not None and current > previous:
        observed["previous_to_current_hours"] = round((current - previous) / 3_600_000, 3)
    if current is not None and following is not None and following > current:
        observed["current_to_next_hours"] = round((following - current) / 3_600_000, 3)
    live_next_interval = observed.get("current_to_next_hours")
    intervals_agree = (
        len(observed) == 2
        and abs(observed["previous_to_current_hours"] - observed["current_to_next_hours"]) <= 0.05
    )
    valid = (
        record.get("http_status") == 200
        and str(record.get("api_code")) == "0"
        and funding.get("instId") == INSTRUMENT_ID
        and following is not None
        and live_next_interval is not None
    )
    if not valid:
        status = "NON VERIFICABILE"
    elif not intervals_agree:
        status = "TIMESTAMP_API_NON_ALLINEATI"
    elif abs(live_next_interval - 4.0) <= 0.05:
        status = "API_4H_COHERENTE_CON_SPECIFICA"
    else:
        status = "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H"
    return {
        "published_reference_interval_hours": 4,
        "published_reference_source_url": "https://www.okx.com/en-gb/help/x-perps-contract-specifications",
        "dynamic_interval_policy_source_url": "https://www.okx.com/en-eu/help/okx-to-enable-automatic-updates-for-funding-fee-settlement-period",
        "live_next_interval_hours": live_next_interval,
        "observed_intervals": observed,
        "status": status,
        "funding_time_ms": current,
        "next_funding_time_ms": following,
        "raw_funding_rate": funding.get("fundingRate"),
        "raw_next_funding_rate": funding.get("nextFundingRate"),
        "method": funding.get("method"),
        "interpretation": (
            "La specifica pubblica OKX OEM/EEA indica un intervallo standard di 4 ore per ZEC X-Perps. OKX documenta anche adeguamenti dinamici della frequenza; i timestamp API live qui osservati sono a 8 ore, ma la causa specifica non è confermata. Il warning resta visibile e nextFundingTime è riportato come osservazione API, senza proiettare funding futuro."
            if status == "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H" else
            "Timestamp API non allineati: nextFundingTime resta un dato grezzo non verificato."
            if status == "TIMESTAMP_API_NON_ALLINEATI" else
            "Intervallo API coerente con la specifica pubblicata."
            if status == "API_4H_COHERENTE_CON_SPECIFICA" else
            "Dati funding insufficienti o non freschi."
        ),
    }


def _market_record(snapshot: dict[str, Any], name: str, expected_id: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    record = (snapshot.get("market") or {}).get(name)
    if not isinstance(record, dict):
        return None, f"mercato {name}: record assente"
    if record.get("http_status") != 200 or str(record.get("api_code")) != "0":
        return None, f"mercato {name}: risposta HTTP/API non valida"
    first = record.get("first")
    if not isinstance(first, dict):
        return None, f"mercato {name}: payload vuoto"
    if expected_id and first.get("instId") != expected_id:
        return None, f"mercato {name}: strumento diverso da {expected_id}"
    exchange_age = _number(record.get("exchange_age_seconds"))
    # OKX instrument metadata rows do not carry an exchange `ts`; freshness is
    # bounded by the bridge retrieval timestamp for that static catalog data.
    if name == "zec_xperp_instrument" and exchange_age is None:
        exchange_age = _number(record.get("age_seconds"))
    if exchange_age is None or exchange_age < -1 or exchange_age > 180:
        return None, f"mercato {name}: timestamp exchange non fresco/verificabile"
    return first, None


def _breakout_early_warning(snapshot: dict[str, Any], x1m: list[dict[str, Any]], x5m: list[dict[str, Any]], oi: dict[str, Any] | None, ws_ready: bool) -> dict[str, Any]:
    """Detect preparation/breakout/retest conditions without changing the entry signal."""
    if len(x5m) < 7:
        return {
            "status": "NON VERIFICABILE",
            "candidate_direction": None,
            "reason": "storico 5m insufficiente per misurare compressione e livelli precedenti",
            "orders_enabled": False,
        }

    def value(row: dict[str, Any], field: str) -> float | None:
        return _number(row.get(field))

    completed = x5m[-1]
    previous = x5m[-4:-1]
    older = x5m[-7:-4]
    close = value(completed, "close")
    highs = [value(row, "high") for row in previous]
    lows = [value(row, "low") for row in previous]
    ranges = [
        max(value(row, "high") - value(row, "low"), 0.0)
        for row in x5m[-7:]
        if value(row, "high") is not None and value(row, "low") is not None
    ]
    if close is None or not highs or not lows or any(v is None for v in highs + lows) or len(ranges) < 6:
        return {
            "status": "NON VERIFICABILE",
            "candidate_direction": None,
            "reason": "OHLC 5m incompleto",
            "orders_enabled": False,
        }

    resistance = max(highs)
    support = min(lows)
    atr_proxy = sum(ranges[-6:]) / 6
    if atr_proxy <= 0:
        return {"status": "NON VERIFICABILE", "candidate_direction": None, "reason": "ATR proxy non valido", "orders_enabled": False}

    volumes = [value(row, "volume") for row in x5m[-6:]]
    volume_ratio = None
    if all(v is not None and v >= 0 for v in volumes[:-1]) and volumes[-1] is not None:
        baseline = sum(volumes[:-1]) / len(volumes[:-1])
        if baseline > 0:
            volume_ratio = round(volumes[-1] / baseline, 3)

    recent_range = max(value(row, "high") for row in previous) - min(value(row, "low") for row in previous)
    older_range = max(value(row, "high") for row in older) - min(value(row, "low") for row in older)
    compression_ratio = round(recent_range / older_range, 3) if older_range > 0 else None

    btc5m, _ = _series(snapshot, "BTC-USDC", "5m")
    zec5m, _ = _series(snapshot, "ZEC-USDC", "5m")
    relative_strength = None
    if _aligned_pair(btc5m, zec5m, x5m):
        zec_return = _price_change(zec5m)
        btc_return = _price_change(btc5m)
        if zec_return is not None and btc_return is not None:
            relative_strength = round((zec_return - btc_return) * 100, 4)

    micro = snapshot.get("microstructure") or {}
    cvd = micro.get("cvd") or {}
    cvd_1m = _number(cvd.get("1m")) if ws_ready else None
    cvd_5m = _number(cvd.get("5m")) if ws_ready else None
    oi_change = oi.get("oi_change_pct") if oi else None

    up_break = close > resistance
    down_break = close < support
    up_distance = (close - resistance) / atr_proxy
    down_distance = (support - close) / atr_proxy
    near_resistance = 0 <= (resistance - close) / atr_proxy <= 0.60
    near_support = 0 <= (close - support) / atr_proxy <= 0.60
    volume_confirmed = volume_ratio is not None and volume_ratio >= 1.50
    volume_building = volume_ratio is not None and volume_ratio >= 1.15
    relative_long = relative_strength is not None and relative_strength >= 0.05
    relative_short = relative_strength is not None and relative_strength <= -0.05
    flow_long = (cvd_1m is not None and cvd_1m > 0) or (cvd_5m is not None and cvd_5m > 0)
    flow_short = (cvd_1m is not None and cvd_1m < 0) or (cvd_5m is not None and cvd_5m < 0)
    compressed = compression_ratio is not None and compression_ratio <= 0.80

    direction = None
    status = "NESSUN SETUP IMMINENTE"
    reasons = []
    if up_break:
        direction = "LONG"
        if volume_confirmed and (relative_long or flow_long):
            status = "BREAKOUT RIALZISTA" if up_distance <= 0.75 else "BREAKOUT GIA' ESTESO — ATTENDI RETEST"
            reasons.append("chiusura 5m sopra la resistenza delle tre barre precedenti")
        else:
            status = "ROTTURA RIALZISTA NON CONFERMATA"
            reasons.append("prezzo sopra la resistenza ma volume/forza relativa/flusso non confermano insieme")
    elif down_break:
        direction = "SHORT"
        if volume_confirmed and (relative_short or flow_short):
            status = "BREAKDOWN RIBASSISTA" if down_distance <= 0.75 else "BREAKDOWN GIA' ESTESO — ATTENDI RETEST"
            reasons.append("chiusura 5m sotto il supporto delle tre barre precedenti")
        else:
            status = "ROTTURA RIBASSISTA NON CONFERMATA"
            reasons.append("prezzo sotto il supporto ma volume/forza relativa/flusso non confermano insieme")
    elif near_resistance and (relative_long or (volume_building and compressed)):
        direction, status = "LONG", "PRE-ALLERTA LONG"
        reasons.append("prezzo vicino alla resistenza con forza relativa o compressione/volume in aumento")
    elif near_support and (relative_short or (volume_building and compressed)):
        direction, status = "SHORT", "PRE-ALLERTA SHORT"
        reasons.append("prezzo vicino al supporto con debolezza relativa o compressione/volume in aumento")

    if volume_ratio is None:
        reasons.append("volume 5m non disponibile: conferma volume non verificabile")
    else:
        reasons.append(f"volume 5m rispetto alle cinque barre precedenti: {volume_ratio:.2f}x")
    if compression_ratio is not None:
        reasons.append(f"rapporto range recente/precedente: {compression_ratio:.2f}")
    if relative_strength is not None:
        reasons.append(f"forza relativa ZEC/BTC 5m: {relative_strength:+.4f}%")
    if oi_change is not None:
        reasons.append(f"variazione OI osservata: {oi_change:+.4f}% (direzione non dedotta da sola)")
    if cvd_1m is not None and cvd_5m is not None:
        reasons.append(f"CVD 1m/5m: {cvd_1m:+.4g}/{cvd_5m:+.4g} contratti")

    return {
        "status": status,
        "candidate_direction": direction,
        "reference_resistance": round(resistance, 6),
        "reference_support": round(support, 6),
        "close_5m": round(close, 6),
        "atr_proxy_5m": round(atr_proxy, 6),
        "distance_from_resistance_atr": round(up_distance, 3),
        "distance_from_support_atr": round(down_distance, 3),
        "volume_ratio_5m": volume_ratio,
        "compression_ratio": compression_ratio,
        "relative_strength_pct": relative_strength,
        "cvd_1m_contracts": cvd_1m,
        "cvd_5m_contracts": cvd_5m,
        "oi_change_pct": oi_change,
        "reasons": reasons,
        "orders_enabled": False,
        "interpretation": "preallerta diagnostica; non è un trigger d'ingresso né una probabilità",
    }


def evaluate_signal(snapshot: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Evaluate one compact radar snapshot without emitting orders or position sizing."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    blockers: list[str] = []
    limitations: list[str] = []

    captured_at = _time(snapshot.get("source_captured_at"))
    source_age = (now - captured_at).total_seconds() if captured_at else None
    if snapshot.get("schema_version") != 3:
        blockers.append("snapshot non schema v3")
    if snapshot.get("source_status") != "ok":
        blockers.append("source_status diverso da ok")
    if captured_at is None or source_age is None or source_age < 0 or source_age > 180:
        blockers.append("snapshot oltre 180 secondi o timestamp non valido")
    if any(value for value in (snapshot.get("read_errors") or {}).values()):
        blockers.append("errori di lettura nel radar snapshot")
    source_mode = snapshot.get("feed_source_mode")
    if source_mode in ("OKX_EEA_REST_SNAPSHOT", "OKX_EEA_REST_WS_SNAPSHOT", "LEGACY_RENDER_WEBSOCKET"):
        if snapshot.get("feed_capture_status") != "ok":
            blockers.append("acquisizione feed non valida")
        health = snapshot.get("feed_health") or {}
        if health.get("status") != "ok":
            blockers.append("diagnostica del feed non valida")
        if source_mode == "OKX_EEA_REST_SNAPSHOT":
            blockers.append("microstruttura WebSocket continua N/D: snapshot REST non prova continuità LIVE")
        elif not health.get("websocket_connected"):
            blockers.append("feed WebSocket non connesso o health non OK")
    else:
        if snapshot.get("render_health_http") != 200 or snapshot.get("render_candles_http") != 200:
            blockers.append("healthz/candles non entrambi HTTP 200")
        health = snapshot.get("render_health") or {}
        if health.get("status") != "ok" or not health.get("websocket_connected"):
            blockers.append("feed WebSocket non connesso o health non OK")

    xperp, xperp_error = _market_record(snapshot, "zec_xperp_ticker", INSTRUMENT_ID)
    if xperp_error:
        blockers.append(xperp_error)
    _spot_zec, zec_error = _market_record(snapshot, "zec_spot_ticker", "ZEC-USDC")
    _spot_btc, btc_error = _market_record(snapshot, "btc_spot_ticker", "BTC-USDC")
    if zec_error:
        blockers.append(zec_error)
    if btc_error:
        blockers.append(btc_error)
    instrument, instrument_error = _market_record(snapshot, "zec_xperp_instrument", INSTRUMENT_ID)
    if instrument_error:
        blockers.append(instrument_error)

    funding_interval = _funding_interval_diagnostic(snapshot)
    if funding_interval["status"] in ("API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H", "TIMESTAMP_API_NON_ALLINEATI"):
        limitations.append("Funding: OKX support conferma settlement ogni 4h; timestamp API discordanti (riportati grezzi, non usati per sostituire la specifica 4h)")

    x1m, x1m_error = _series(snapshot, FEED_KEY, "1m", reference=now)
    x5m, x5m_error = _series(snapshot, FEED_KEY, "5m", reference=now)
    btc1m, btc1m_error = _series(snapshot, "BTC-USDC", "1m", reference=now)
    zec1m, zec1m_error = _series(snapshot, "ZEC-USDC", "1m", reference=now)
    btc5m, btc5m_error = _series(snapshot, "BTC-USDC", "5m", reference=now)
    zec5m, zec5m_error = _series(snapshot, "ZEC-USDC", "5m", reference=now)
    for error in (x1m_error, x5m_error, btc1m_error, zec1m_error, btc5m_error, zec5m_error):
        if error:
            blockers.append(error)

    for bar, rows_by_market in (
        ("1m", (x1m, btc1m, zec1m)),
        ("5m", (x5m, btc5m, zec5m)),
    ):
        if all(rows_by_market):
            if not _aligned_pair(*rows_by_market):
                blockers.append(f"coerenza temporale {bar} non valida tra BTC spot, ZEC spot e ZEC X-Perp")

    micro = snapshot.get("microstructure") or {}
    websocket = micro.get("websocket") or {}
    cvd = micro.get("cvd") or {}
    book = micro.get("book_imbalance") or {}
    ws_ready = (
        websocket.get("connected") is True
        and websocket.get("book_valid") is True
        and _number(websocket.get("message_age_seconds")) is not None
        and 0 <= _number(websocket.get("message_age_seconds")) <= 120
        and _number(websocket.get("trade_age_seconds")) is not None
        and 0 <= _number(websocket.get("trade_age_seconds")) <= 180
        and _number(websocket.get("book_age_seconds")) is not None
        and 0 <= _number(websocket.get("book_age_seconds")) <= 180
        and cvd.get("ready_1m") is True
        and cvd.get("ready_5m") is True
        and _number(cvd.get("1m")) is not None
        and _number(cvd.get("5m")) is not None
    )
    if not ws_ready:
        blockers.append("microstruttura WebSocket/CVD 1m/5m non pronta o stantia")

    groups: dict[str, dict[str, Any]] = {}
    factors: list[str] = []
    contrary: list[str] = []

    if ws_ready and x1m and x5m:
        cvd1 = _sign(_number(cvd.get("1m")))
        cvd5 = _sign(_number(cvd.get("5m")))
        cvd_score = (cvd1 + cvd5) / 2
        imbalance = _number(book.get("imbalance_top10"))
        book_score = _clamp(imbalance / 0.35) if imbalance is not None else 0.0
        capture_ms = captured_at.timestamp() * 1000
        book_history = []
        for item in websocket.get("book_history") or []:
            if not isinstance(item, dict):
                continue
            timestamp = _number(item.get("ts_ms"))
            imbalance_at = _number(item.get("imbalance_top10"))
            if timestamp is not None and imbalance_at is not None and 0 <= capture_ms - timestamp <= 180_000:
                book_history.append((timestamp, imbalance_at))
        book_delta_score = 0.0
        book_delta_text = "dinamica imbalance book: storico breve non disponibile"
        if len(book_history) >= 2:
            first, last = min(book_history), max(book_history)
            elapsed = (last[0] - first[0]) / 1000
            if elapsed >= 1:
                delta = last[1] - first[1]
                book_delta_score = _clamp(delta / 0.30)
                book_delta_text = f"dinamica imbalance top10: {delta:+.3f} in {elapsed:.0f}s"
        return1 = _price_change(x1m)
        return5 = _price_change(x5m)
        price_score = 0.0
        absorption = "nessuna divergenza CVD/prezzo evidente nelle ultime barre chiuse"
        if cvd5 > 0 and return5 is not None and return5 < -0.0003:
            price_score, absorption = -0.4, "possibile assorbimento degli acquisti: CVD positivo senza risposta rialzista del prezzo"
        elif cvd5 < 0 and return5 is not None and return5 > 0.0003:
            price_score, absorption = 0.4, "possibile assorbimento delle vendite: CVD negativo senza risposta ribassista del prezzo"
        else:
            price_score = _sign(return1) * 0.15 if return1 is not None else 0.0
        flow_score = _clamp(0.55 * cvd_score + 0.20 * book_score + 0.15 * book_delta_score + 0.10 * price_score)
        flow_factors = [
            f"CVD continuo 1m/5m: {float(cvd['1m']):+.4g}/{float(cvd['5m']):+.4g} contratti",
            f"imbalance top10: {imbalance:+.3f}" if imbalance is not None else "imbalance top10 n/d",
            book_delta_text,
            absorption,
        ]
        groups["order_flow"] = {"score": round(flow_score, 4), "weight": WEIGHTS["order_flow"], "factors": flow_factors}
        factors.extend(flow_factors[:2])
        if absorption.startswith("possibile assorbimento"):
            contrary.append(absorption)

    if x1m and x5m:
        direction1, direction5 = _candlestick_direction(x1m), _candlestick_direction(x5m)
        previous = x5m[-4:-1]
        high_values = [_number(row.get("high")) for row in previous]
        low_values = [_number(row.get("low")) for row in previous]
        last_close = _number(x5m[-1].get("close"))
        breakout = 0.0
        breakout_text = "nessuna rottura del massimo/minimo delle tre barre 5m precedenti"
        if last_close is not None and high_values and all(value is not None for value in high_values) and last_close > max(high_values):
            breakout, breakout_text = 0.8, "chiusura 5m oltre il massimo delle tre barre precedenti"
        elif last_close is not None and low_values and all(value is not None for value in low_values) and last_close < min(low_values):
            breakout, breakout_text = -0.8, "chiusura 5m sotto il minimo delle tre barre precedenti"
        direction_score = (direction1 + direction5) / 2
        micro_score = _clamp(0.55 * direction_score + 0.45 * breakout)
        candle_factors = [
            f"chiusure 1m/5m: {'rialziste' if direction1 > 0 else 'ribassiste' if direction1 < 0 else 'neutrali'}/{'rialziste' if direction5 > 0 else 'ribassiste' if direction5 < 0 else 'neutrali'}",
            breakout_text,
        ]
        groups["microstructure"] = {"score": round(micro_score, 4), "weight": WEIGHTS["microstructure"], "factors": candle_factors}
        factors.extend(candle_factors)
        if direction1 != direction5:
            contrary.append("direzione delle chiusure 1m e 5m non allineata")

    if _aligned_pair(btc5m, zec5m, x5m):
        zec_return = _price_change(zec5m)
        btc_return = _price_change(btc5m)
        relative = zec_return - btc_return if zec_return is not None and btc_return is not None else None
        if relative is not None:
            relative_score = _clamp(relative / 0.005)
            label = "sovraperforma BTC" if relative > 0.0005 else "sottoperforma BTC" if relative < -0.0005 else "in linea con BTC"
            relative_factor = f"forza relativa ZEC/BTC 5m: {relative * 100:+.3f}% ({label})"
            groups["relative_strength"] = {"score": round(relative_score, 4), "weight": WEIGHTS["relative_strength"], "factors": [relative_factor]}
            factors.append(relative_factor)
    elif btc5m and zec5m and x5m:
        blockers.append("forza relativa 5m ZEC/BTC non allineata o non disponibile")

    oi, oi_error = _open_interest_signal(snapshot)
    if oi is not None:
        groups["positioning"] = {"score": oi["score"], "weight": WEIGHTS["positioning"], "factors": [oi["interpretation"]], "details": oi}
        factors.append(oi["interpretation"])
    else:
        # OI/liquidations add context when present, but are optional providers.
        # Missing OI lowers confidence; it does not invalidate otherwise sound
        # independent price, flow and relative-strength evidence.
        limitations.append(oi_error or "posizionamento OI non disponibile")

    valid_group_names = [name for name in WEIGHTS if name in groups]
    weight_sum = sum(groups[name]["weight"] for name in valid_group_names)
    net = (
        sum(groups[name]["score"] * groups[name]["weight"] for name in valid_group_names) / weight_sum
        if weight_sum else 0.0
    )
    net = _clamp(net)
    direction = "LONG" if net >= 0.18 else "SHORT" if net <= -0.18 else "NEUTRA"
    exploratory_direction = "LONG" if net > 0 else "SHORT" if net < 0 else None
    direction_sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else 0
    aligned_groups = sum(1 for group in groups.values() if direction_sign and _sign(group["score"]) == direction_sign)
    agreement = aligned_groups / len(groups) if groups else 0.0
    confidence = round(100 * min(0.80, (len(groups) / len(WEIGHTS)) * 0.45 + agreement * 0.35))
    if "positioning" not in groups:
        confidence = min(confidence, 65)
    if any(value in blockers for value in (x1m_error, x5m_error, btc5m_error, zec5m_error) if value):
        confidence = min(confidence, 40)
    groups_ready = all(name in groups for name in ("order_flow", "microstructure", "relative_strength"))
    candles_aligned = bool(x1m and x5m and _candlestick_direction(x1m) == _candlestick_direction(x5m) == direction_sign and direction_sign)
    confirmed = (
        not blockers
        and groups_ready
        and direction_sign != 0
        and abs(net) >= 0.68
        and agreement >= 0.75
        and candles_aligned
    )
    if blockers or not groups_ready or len(groups) < 3 or not ws_ready or xperp is None or instrument is None:
        stage = "ATTENDERE"
    elif confirmed:
        stage = "TRIGGER CONFERMATO"
    elif abs(net) >= 0.45 and len(groups) >= 3:
        stage = "SETUP IN FORMAZIONE"
    elif abs(net) >= 0.20 and len(groups) >= 2:
        stage = "PRE-ALLERTA"
    else:
        stage = "ATTENDERE"
    decision = direction if stage == "TRIGGER CONFERMATO" else "ATTENDERE"

    valid_until = None
    if not blockers and groups_ready and ws_ready:
        # Anchor expiry to the underlying observations, not to publication time.
        # Book/trade ages are measured at capture and cannot be reset by GitHub.
        candle_deadlines = [
            _time(rows[-1]["completed_at"]) + timedelta(seconds=180)
            for rows in (x1m, x5m, btc1m, btc5m, zec1m, zec5m)
        ]
        websocket_deadlines = [
            captured_at + timedelta(seconds=limit - _number(websocket[key]))
            for key, limit in (("message_age_seconds", 120), ("trade_age_seconds", 180), ("book_age_seconds", 180))
        ]
        valid_until = min(candle_deadlines + websocket_deadlines).isoformat()

    if direction_sign:
        for name, group in groups.items():
            if _sign(group["score"]) == direction_sign and group["score"] != 0:
                factors_text = group.get("factors") or []
                factors.append(f"a favore {direction}: {name} ({'; '.join(factors_text)})")
            elif _sign(group["score"]) == -direction_sign:
                contrary.append(f"{name} contrasta il candidato {direction}")

    latest_price = _number(x1m[-1].get("close")) if x1m else None
    swing = x1m[-6:] if x1m else []
    invalidation = None
    if latest_price is not None and swing:
        if direction == "LONG":
            levels = [_number(row.get("low")) for row in swing]
            level = min(value for value in levels if value is not None) if any(value is not None for value in levels) else None
            if level is not None:
                invalidation = {"direction": "LONG", "price": level, "condition": "chiusura 1m sotto il minimo strutturale delle ultime barre completate"}
        elif direction == "SHORT":
            levels = [_number(row.get("high")) for row in swing]
            level = max(value for value in levels if value is not None) if any(value is not None for value in levels) else None
            if level is not None:
                invalidation = {"direction": "SHORT", "price": level, "condition": "chiusura 1m sopra il massimo strutturale delle ultime barre completate"}

    breakout_warning = _breakout_early_warning(snapshot, x1m, x5m, oi, ws_ready)

    contract_safety = build_contract_safety(instrument, verified=not bool(instrument_error))
    contract_safety["funding_interval"] = funding_interval

    return {
        "schema_version": 1,
        "engine": "ZEC Early Signal Engine",
        "mode": "simulation_only",
        "instrument": {"venue": "OKX EEA", "inst_id": INSTRUMENT_ID, "feed_key": FEED_KEY, "metadata_verified": instrument is not None},
        "captured_at": snapshot.get("source_captured_at"),
        "status": stage,
        "decision": decision,
        "valid_until": valid_until,
        "candidate_direction": direction,
        "exploratory_direction": exploratory_direction,
        "scores": {
            "net": round(net * 100, 1),
            "long": round(max(0.0, net) * 100, 1),
            "short": round(max(0.0, -net) * 100, 1),
            "unit": "heuristic points, not win probability",
            "groups": groups,
        },
        "confidence": {"score": confidence, "label": "alta" if confidence >= 70 else "media" if confidence >= 45 else "bassa", "calibrated": False},
        "favorable_signals": list(dict.fromkeys(factors)),
        "contrary_signals": list(dict.fromkeys(contrary)),
        "invalidation": invalidation,
        "breakout_early_warning": breakout_warning,
        "data_quality": {
            "snapshot_age_seconds": round(source_age, 1) if source_age is not None else None,
            "websocket_ready": ws_ready,
            "groups_available": valid_group_names,
            "required_groups_ready": groups_ready,
            "blockers": list(dict.fromkeys(blockers)),
            "limitations": list(dict.fromkeys(limitations)),
            "liquidations": "non disponibili nel feed corrente; nessun contributo al punteggio",
        },
        "contract_safety": contract_safety,
        "execution": {
            "orders_enabled": False,
            "position_quantity": None,
            "margin": None,
            "pnl": None,
            "sizing_calculated": False,
        },
    }
