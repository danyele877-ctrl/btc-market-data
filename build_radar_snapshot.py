#!/usr/bin/env python3
"""Build a compact, machine-readable radar snapshot from verified bridge files."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zec_early_signal import evaluate_signal
from zec_shadow_signal import evaluate_shadow_signal
from zec_predictive_engine import evaluate_predictive_engine
from zec_context_archive import update_context_archive
from zec_prediction_tracking import update_prediction_tracking
from zec_contract_config import build_contract_safety
from capture_render_feed import validate_feed

ROOT = Path("data")
FEED = ROOT / "render_feed.json"
MICRO = ROOT / "zec_microstructure_history.json"
HISTORY = ROOT / "zec_xperp_history.json"
OUT = ROOT / "radar_snapshot.json"
BAR_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


def write_json_atomically(path, payload):
    """Write a complete JSON snapshot before replacing the last known-good file."""
    path = Path(path)
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


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}"}


def first_payload(record):
    payload = record.get("payload") if isinstance(record, dict) else None
    rows = payload.get("data") if isinstance(payload, dict) else None
    return rows[0] if isinstance(rows, list) and rows else None


def candle_tail(feed, key, bar, count=8):
    try:
        rows = feed["candles"]["payload"]["instruments"][key][bar]
        return rows[-count:] if isinstance(rows, list) else []
    except Exception:
        return []


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def ema_series(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1.0 - alpha)
        result[index] = previous
    return result


def wilder_average(values, period):
    if len(values) < period:
        return None
    average = sum(values[:period]) / period
    for value in values[period:]:
        average = ((average * (period - 1)) + value) / period
    return average


def indicator_pack(candles):
    """Calculate common indicators from ordered, gap-free completed OHLC candles."""
    try:
        closes = [float(item["close"]) for item in candles]
        highs = [float(item["high"]) for item in candles]
        lows = [float(item["low"]) for item in candles]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) < 200:
        return None

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(value, 0.0) for value in changes]
    losses = [max(-value, 0.0) for value in changes]
    avg_gain = wilder_average(gains, 14)
    avg_loss = wilder_average(losses, 14)
    if avg_gain is None or avg_loss is None:
        rsi14 = None
    elif avg_loss == 0:
        rsi14 = 100.0 if avg_gain > 0 else 50.0
    else:
        rsi14 = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_values = [
        (ema12[i] - ema26[i]) if ema12[i] is not None and ema26[i] is not None else None
        for i in range(len(closes))
    ]
    first_macd = next((i for i, value in enumerate(macd_values) if value is not None), None)
    macd_line = macd_values[-1]
    signal_value = None
    histogram = None
    if first_macd is not None:
        compact_macd = [value for value in macd_values[first_macd:] if value is not None]
        signal_series = ema_series(compact_macd, 9)
        signal_value = signal_series[-1]
        if macd_line is not None and signal_value is not None:
            histogram = macd_line - signal_value

    true_ranges = []
    for i in range(1, len(closes)):
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr14 = wilder_average(true_ranges, 14)

    return {
        "ema20": round(ema20, 8) if ema20 is not None else None,
        "ema50": round(ema50, 8) if ema50 is not None else None,
        "ema200": round(ema200, 8) if ema200 is not None else None,
        "rsi14": round(rsi14, 4) if rsi14 is not None else None,
        "macd": round(macd_line, 8) if macd_line is not None else None,
        "macd_signal": round(signal_value, 8) if signal_value is not None else None,
        "macd_histogram": round(histogram, 8) if histogram is not None else None,
        "atr14": round(atr14, 8) if atr14 is not None else None,
    }


def summarize_history(record, bar, now):
    candles = record.get("candles", []) if isinstance(record, dict) else []
    candles = sorted(
        [item for item in candles if isinstance(item, dict) and item.get("ts_ms") is not None],
        key=lambda item: int(item["ts_ms"]),
    )
    interval = BAR_SECONDS.get(bar)
    gaps = sum(
        1 for previous, current in zip(candles, candles[1:])
        if interval and int(current["ts_ms"]) - int(previous["ts_ms"]) != interval * 1000
    )
    newest = candles[-1] if candles else None
    completed = parse_time(newest.get("completed_at")) if newest else None
    age = (now - completed).total_seconds() if completed else None
    # Historical inputs are usable only when the request explicitly returned
    # HTTP 200. Missing status metadata must never be treated as success.
    http_ok = record.get("http_status") == 200 and not record.get("error")
    ready = (
        http_ok and len(candles) >= 200 and gaps == 0
        and age is not None and -60 <= age <= (interval * 3 if interval else 0)
    )
    return {
        "http_status": record.get("http_status"),
        "count": len(candles),
        "gap_count": gaps,
        "ready_200": ready,
        "oldest_completed_at": candles[0].get("completed_at") if candles else None,
        "newest_completed_at": newest.get("completed_at") if newest else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "last_close": newest.get("close") if newest else None,
        "indicators": indicator_pack(candles) if ready else None,
        "error": record.get("error"),
    }


def microstructure_summary(micro):
    books = micro.get("book_snapshots", []) if isinstance(micro, dict) else []
    trades = micro.get("trades", []) if isinstance(micro, dict) else []
    sorted_trades = sorted(trades, key=lambda item: (int(item.get("ts_ms", 0)), int(item.get("tradeId", 0))))
    rest_trades = [item for item in sorted_trades if item.get("source", "rest") == "rest"]
    websocket_trades = [item for item in sorted_trades if item.get("source") == "websocket"]
    rest_ids = {str(item.get("tradeId")) for item in rest_trades if str(item.get("tradeId", "")).isdigit()}
    websocket_ids = {str(item.get("tradeId")) for item in websocket_trades if str(item.get("tradeId", "")).isdigit()}
    # WS gap_before is computed from the live channel sequence, including OKX
    # aggregated-trade count semantics. REST polling is sampled and cannot
    # prove continuity from adjacent tradeId values.
    websocket_trade_gaps = sum(1 for item in websocket_trades if item.get("gap_before") is True)
    sorted_books = sorted(books, key=lambda item: int(item.get("ts_ms", 0)))
    websocket_books = sorted(
        (item for item in books if item.get("source") == "websocket"),
        key=lambda item: (int(item.get("ts_ms", 0)), int(item.get("seq_id", 0))),
    )
    books_by_timestamp = {}
    for item in books:
        books_by_timestamp.setdefault(int(item.get("ts_ms", 0)), []).append(item)
    cross_source_timestamp_groups = sum(
        1 for rows in books_by_timestamp.values()
        if len({row.get("source", "rest") for row in rows}) > 1
    )
    same_source_timestamp_groups = sum(
        1 for rows in books_by_timestamp.values()
        if any(sum(row.get("source", "rest") == source for row in rows) > 1
               for source in {row.get("source", "rest") for row in rows})
    )
    intervals = [
        (int(current["ts_ms"]) - int(previous["ts_ms"])) / 1000.0
        for previous, current in zip(websocket_books, websocket_books[1:])
        if int(current.get("ts_ms", 0)) > int(previous.get("ts_ms", 0))
    ]
    median_interval = None
    if intervals:
        ordered = sorted(intervals)
        middle = len(ordered) // 2
        median_interval = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    last_book = sorted_books[-1] if sorted_books else None
    last_trade = sorted_trades[-1] if sorted_trades else None
    oi_history = micro.get("open_interest_history", []) if isinstance(micro, dict) else []
    oi_history = [item for item in oi_history if isinstance(item, dict) and item.get("inst_id") == "ZEC-USD_UM_XPERP-310530"]
    return {
        "schema_version": micro.get("schema_version"),
        "instrument": micro.get("instrument"),
        "rest_history_collection_method": "REST polling",
        "book_snapshot_count": len(books),
        "rest_book_snapshot_count": sum(1 for item in books if item.get("source", "rest") == "rest"),
        "websocket_book_snapshot_count": len(websocket_books),
        "book_duplicate_timestamp_collisions": max(0, len(books) - len({int(item.get("ts_ms", 0)) for item in books})),
        "book_cross_source_timestamp_groups": cross_source_timestamp_groups,
        "book_same_source_timestamp_groups": same_source_timestamp_groups,
        "book_sample_median_seconds": round(median_interval, 1) if median_interval is not None else None,
        "last_book": last_book,
        "previous_book": sorted_books[-2] if len(sorted_books) > 1 else None,
        "last_book_captured_at": last_book.get("captured_at") if last_book else None,
        "trade_count": len(trades),
        "rest_trade_count": len(rest_trades),
        "websocket_trade_count": len(websocket_trades),
        "unique_trade_ids": len(rest_ids | websocket_ids),
        "rest_unique_trade_ids": len(rest_ids),
        "websocket_unique_trade_ids": len(websocket_ids),
        "trade_id_gap_events": websocket_trade_gaps,
        "websocket_trade_gap_events": websocket_trade_gaps,
        "rest_trade_gap_events": None,
        "first_trade": sorted_trades[0] if sorted_trades else None,
        "last_trade": last_trade,
        "trade_window_start": sorted_trades[0].get("ts_ms") if sorted_trades else None,
        "trade_window_end": last_trade.get("ts_ms") if last_trade else None,
        "rest_history_cvd_status": "N/D: REST polling campionato; continuità tradeId non dimostrabile",
        "websocket_archive_continuity_status": "N/D: gap rilevati nel flusso archiviato" if websocket_trade_gaps else "N/D: archivio campionato; continuità LIVE si legge solo dallo stato WebSocket",
        "book_sequence_status": "N/D: archivio WS campionato; integrità sequenza verificata dal servizio LIVE",
        "open_interest_history": oi_history,
    }


def funding_diagnostics(record, now):
    """Use OKX's own fundingTime/nextFundingTime interval for the next event.

    The published X-Perps table is retained as a reference, but OKX API
    documentation says the live interval can vary and should be read from
    fundingTime and nextFundingTime.
    """
    first = record.get("first") if isinstance(record, dict) else None
    first = first if isinstance(first, dict) else {}

    def timestamp_ms(field):
        try:
            value = int(first.get(field))
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    def interval_hours(earlier, later):
        if earlier is None or later is None or later <= earlier:
            return None
        return round((later - earlier) / 3_600_000, 2)

    def iso_from_ms(value):
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")

    previous_ms = timestamp_ms("prevFundingTime")
    funding_ms = timestamp_ms("fundingTime")
    next_ms = timestamp_ms("nextFundingTime")
    previous_interval = interval_hours(previous_ms, funding_ms)
    next_interval = interval_hours(funding_ms, next_ms)
    try:
        exchange_age = float(record.get("exchange_age_seconds")) if isinstance(record, dict) else None
    except (TypeError, ValueError):
        exchange_age = None
    http_ok = (
        isinstance(record, dict) and record.get("http_status") == 200
        and str(record.get("api_code")) == "0"
        and exchange_age is not None and -1 <= exchange_age <= 180 and bool(first)
        and first.get("instId") == "ZEC-USD_UM_XPERP-310530"
    )
    observed = [value for value in (previous_interval, next_interval) if value is not None]
    intervals_agree = (
        len(observed) == 2 and abs(observed[0] - observed[1]) <= 0.05
    )
    if not http_ok or next_ms is None or next_interval is None:
        status = "N_D_NON_VERIFICABILE"
    elif intervals_agree and abs(next_interval - 4.0) <= 0.05:
        status = "API_4H_COHERENTE_CON_SPECIFICA"
    elif intervals_agree:
        status = "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H"
    else:
        status = "TIMESTAMP_API_NON_ALLINEATI"
    return {
        "status": status,
        "instrument_id": first.get("instId"),
        "source": "OKX EEA GET /public/funding-rate",
        "http_status": record.get("http_status") if isinstance(record, dict) else None,
        "api_code": record.get("api_code") if isinstance(record, dict) else None,
        "exchange_age_seconds": exchange_age,
        "published_reference_interval_hours": 4,
        "published_reference_source": "OKX Europe X-Perps Contract Specifications",
        "live_interval_source": "Timestamp API grezzi, non sostituiscono la periodicità ufficiale di 4h confermata da OKX support",
        "prevFundingTime": first.get("prevFundingTime"),
        "fundingTime": first.get("fundingTime"),
        "nextFundingTime": first.get("nextFundingTime"),
        "prev_to_funding_hours_observed": previous_interval,
        "funding_to_next_hours_observed": next_interval,
        "live_next_interval_hours": next_interval,
        "prev_funding_at": iso_from_ms(previous_ms),
        "funding_at": iso_from_ms(funding_ms),
        "next_funding_at": iso_from_ms(next_ms),
        "funding_rate": first.get("fundingRate"),
        "next_funding_rate": first.get("nextFundingRate"),
        "settled_funding_rate": first.get("settFundingRate"),
        "settlement_state": first.get("settState"),
        "method": first.get("method"),
        "api_response_ts": first.get("ts"),
        "interpretation": (
            "La prossima scadenza operativa è quella indicata da nextFundingTime. "
            "I timestamp API grezzi differiscono dalla periodicità di 4h confermata da OKX support; "
            "conservare nextFundingTime come dato API non riconciliato, senza sostituire la periodicità confermata. "
            "Non annualizzare né proiettare il funding futuro."
            if status == "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H" else
            "I timestamp API non sono coerenti tra loro: usare solo nextFundingTime grezzo come dato non verificato, "
            "senza assumere una cadenza o stimare costi futuri."
            if status == "TIMESTAMP_API_NON_ALLINEATI" else
            "Intervallo API coerente con la specifica pubblicata."
            if status == "API_4H_COHERENTE_CON_SPECIFICA" else
            "Dati funding insufficienti o non freschi: nessuna proiezione."
        ),
    }


def main():
    feed, micro, hist = read(FEED), read(MICRO), read(HISTORY)
    now = datetime.now(timezone.utc)
    feed_problems = validate_feed(feed, current_time=now)
    if feed_problems:
        raise SystemExit("Refusing to build radar snapshot from invalid/stale feed: " + "; ".join(feed_problems))
    market = feed.get("okx_market", {}) if isinstance(feed, dict) else {}
    hist_bars = hist.get("bars", {}) if isinstance(hist, dict) else {}
    history_retrieved = parse_time(hist.get("retrieved_at")) if isinstance(hist, dict) else None
    if (
        not isinstance(hist, dict)
        or hist.get("instrument") != "ZEC-USD_UM_XPERP-310530"
        or history_retrieved is None
        or (now - history_retrieved).total_seconds() < 0
        or (now - history_retrieved).total_seconds() > 20 * 60
    ):
        raise SystemExit("Refusing to build radar snapshot from missing, stale or wrong-instrument X-Perp history")
    feed_history_bars = (feed.get("zec_xperp_history") or {}).get("bars", {})
    for bar in ("1m", "5m", "15m", "1H", "4H"):
        record = hist_bars.get(bar, {})
        summary = feed_history_bars.get(bar, {})
        candles = record.get("candles", []) if isinstance(record, dict) else []
        newest = candles[-1].get("completed_at") if candles and isinstance(candles[-1], dict) else None
        if (
            not isinstance(record, dict)
            or record.get("http_status") != 200
            or len(candles) < 200
            or record.get("instrument") not in (None, "ZEC-USD_UM_XPERP-310530")
            or summary.get("ready_200") is not True
            or int(record.get("count") or 0) != int(summary.get("count") or -1)
            or int(record.get("gap_count", -1)) != int(summary.get("gap_count", -1))
            or newest != summary.get("newest_completed_at")
        ):
            raise SystemExit(f"Refusing to build radar snapshot: X-Perp {bar} history file and feed summary disagree or are incomplete")

    candles = {}
    for key in ("BTC-USDC", "ZEC-USDC", "ZEC-USD_UM_XPERP"):
        candles[key] = {}
        for bar in ("1m", "5m"):
            tail = candle_tail(feed, key, bar)
            candles[key][bar] = {
                "tail_count": len(tail),
                "tail": tail[-8:],
                "latest": tail[-1] if tail else None,
                "previous": tail[-2] if len(tail) > 1 else None,
            }

    candles_15m = {}
    for key in ("BTC-USDC", "ZEC-USDC", "ZEC-USD_UM_XPERP"):
        tail = candle_tail(feed, key, "15m")
        candles_15m[key] = {
            "tail_count": len(tail),
            "tail": tail[-8:],
            "latest": tail[-1] if tail else None,
            "previous": tail[-2] if len(tail) > 1 else None,
        }

    market_out = {}
    for name, record in market.items():
        first = first_payload(record)
        payload = record.get("payload") if isinstance(record, dict) else None
        exchange_ts = first.get("ts") if isinstance(first, dict) else None
        exchange_age = None
        try:
            exchange_age = round((now.timestamp() - int(exchange_ts) / 1000), 1) if exchange_ts else None
        except (TypeError, ValueError):
            pass
        market_out[name] = {
            "http_status": record.get("http_status"),
            "api_code": payload.get("code") if isinstance(payload, dict) else None,
            "retrieved_at": record.get("retrieved_at"),
            "age_seconds": (
                round((now - parse_time(record.get("retrieved_at"))).total_seconds(), 1)
                if parse_time(record.get("retrieved_at")) else None
            ),
            "exchange_timestamp": exchange_ts,
            "exchange_age_seconds": exchange_age,
            "error": record.get("error"),
            "first": first,
        }

    xperp_history = {
        bar: summarize_history(item, bar, now)
        for bar, item in hist_bars.items()
    }
    spot_history = {}
    okx_history = feed.get("okx_history", {}) if isinstance(feed, dict) else {}
    for instrument in ("BTC-USDC", "ZEC-USDC"):
        source = okx_history.get(instrument, {})
        spot_history[instrument] = {
            bar: summarize_history(source.get(bar, {}), bar, now)
            for bar in ("1H", "4H")
        }

    health_payload = (feed.get("healthz") or {}).get("payload", {})
    ws_micro_full = health_payload.get("microstructure", {}) if isinstance(health_payload, dict) else {}
    if not ws_micro_full:
        candle_payload = (feed.get("candles") or {}).get("payload", {})
        ws_micro_full = candle_payload.get("microstructure", {}) if isinstance(candle_payload, dict) else {}
    # Raw trade tape and full depth history are persisted separately in
    # zec_microstructure_history.json; keep the compact radar snapshot small.
    ws_micro = dict(ws_micro_full) if isinstance(ws_micro_full, dict) else {}
    ws_micro.pop("trade_events", None)
    ws_micro.pop("book_depth_history", None)
    render_health = dict(health_payload) if isinstance(health_payload, dict) else {}
    render_micro = dict(render_health.get("microstructure") or {})
    render_micro.pop("trade_events", None)
    render_micro.pop("book_depth_history", None)
    if render_micro:
        render_health["microstructure"] = render_micro
    micro_out = microstructure_summary(micro)
    micro_out["websocket"] = ws_micro
    micro_out["cvd"] = ws_micro.get("cvd")
    micro_out["book_imbalance"] = (ws_micro.get("book") or {})
    if ws_micro:
        cvd = ws_micro.get("cvd") or {}
        # Keep REST-polled history diagnostics separate from the live WebSocket CVD.
        gap_counts_verified = all(
            isinstance(cvd.get(f"gap_count_{label}"), int)
            for label in ("1m", "5m", "15m")
        )
        short_windows_gap_free = (
            gap_counts_verified
            and cvd.get("gap_count_1m") == 0
            and cvd.get("gap_count_5m") == 0
        )
        trade_tape_complete = ws_micro.get("trade_events_truncated") is False
        cvd_ready_short = (
            bool(cvd.get("ready_1m") and cvd.get("ready_5m"))
            and short_windows_gap_free
            and trade_tape_complete
        )
        cvd_ready_full = (
            cvd_ready_short
            and bool(cvd.get("ready_15m"))
            and cvd.get("gap_count_15m") == 0
        )
        if cvd_ready_full:
            micro_out["websocket_cvd_status"] = "OK: WebSocket continuo verificato su 1m/5m/15m; zero gap nelle finestre"
        elif cvd_ready_short:
            micro_out["websocket_cvd_status"] = "PARZIALE: WebSocket continuo verificato su 1m/5m; 15m non pronto"
        else:
            micro_out["websocket_cvd_status"] = "N/D: gap, tape troncato o continuità WebSocket non dimostrata"
        micro_out["cvd_status"] = micro_out["websocket_cvd_status"]
        micro_out["book_sequence_status"] = (
            "OK: catena WebSocket LIVE valida; archivio persistito campionato"
            if ws_micro.get("book_valid") else "N/D: book non valido o in attesa di snapshot"
        )
    output = {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_captured_at": feed.get("captured_at"),
        "source_status": feed.get("status"),
        "render_health_http": (feed.get("healthz") or {}).get("http_status"),
        "render_candles_http": (feed.get("candles") or {}).get("http_status"),
        "render_health": render_health,
        "market": market_out,
        "funding_diagnostics": funding_diagnostics(market_out.get("zec_xperp_funding"), now),
        "zec_contract_safety": build_contract_safety(
            market_out.get("zec_xperp_instrument", {}).get("first"),
            verified=(
                market_out.get("zec_xperp_instrument", {}).get("http_status") == 200
                and str(market_out.get("zec_xperp_instrument", {}).get("api_code")) == "0"
                and market_out.get("zec_xperp_instrument", {}).get("age_seconds") is not None
                and -1 <= market_out.get("zec_xperp_instrument", {}).get("age_seconds") <= 180
            ),
            comparison_instrument=(
                market_out.get("zec_xperp_instrument_global", {}).get("first")
                if market_out.get("zec_xperp_instrument_global", {}).get("http_status") == 200
                and str(market_out.get("zec_xperp_instrument_global", {}).get("api_code")) == "0"
                and market_out.get("zec_xperp_instrument_global", {}).get("age_seconds") is not None
                and -1 <= market_out.get("zec_xperp_instrument_global", {}).get("age_seconds") <= 180
                else None
            ),
        ),
        "candles_1m_5m": candles,
        "candles_15m": candles_15m,
        "zec_xperp_history": xperp_history,
        "predictive_history": {
            # Prefer longer Render completed-candle series for intraday outcome paths;
            # use the OKX history archive only as a fallback.
            "xperp_1m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USD_UM_XPERP", {}).get("1m", []) or hist_bars.get("1m", {}).get("candles", []),
            "xperp_5m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USD_UM_XPERP", {}).get("5m", []) or hist_bars.get("5m", {}).get("candles", []),
            "xperp_15m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USD_UM_XPERP", {}).get("15m", []) or hist_bars.get("15m", {}).get("candles", []),
            "xperp_1H": hist_bars.get("1H", {}).get("candles", []),
            "xperp_4H": hist_bars.get("4H", {}).get("candles", []),
            "btc_1m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("BTC-USDC", {}).get("1m", []),
            "btc_5m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("BTC-USDC", {}).get("5m", []),
            "btc_15m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("BTC-USDC", {}).get("15m", []),
            "zec_1m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USDC", {}).get("1m", []),
            "zec_5m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USDC", {}).get("5m", []),
            "zec_15m": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USDC", {}).get("15m", []),
            "btc_1H": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("BTC-USDC", {}).get("1H", []) or okx_history.get("BTC-USDC", {}).get("1H", {}).get("candles", []),
            "btc_4H": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("BTC-USDC", {}).get("4H", []) or okx_history.get("BTC-USDC", {}).get("4H", {}).get("candles", []),
            "zec_1H": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USDC", {}).get("1H", []) or okx_history.get("ZEC-USDC", {}).get("1H", {}).get("candles", []),
            "zec_4H": (((feed.get("candles") or {}).get("payload") or {}).get("instruments") or {}).get("ZEC-USDC", {}).get("4H", []) or okx_history.get("ZEC-USDC", {}).get("4H", {}).get("candles", []),
        },
        "spot_history": spot_history,
        "microstructure": micro_out,
        "read_errors": {
            "feed": feed.get("_read_error"),
            "microstructure": micro.get("_read_error"),
            "history": hist.get("_read_error"),
        },
    }
    output["zec_early_signal"] = evaluate_signal(output, now=now)
    output["zec_shadow_signal"] = evaluate_shadow_signal(
        output, baseline=output["zec_early_signal"], now=now
    )
    # Mirror the independent regional contract comparison into the signal payload;
    # it remains diagnostic and cannot alter any execution/sizing safety flag.
    output["zec_early_signal"]["contract_safety"].update({
        key: value for key, value in output["zec_contract_safety"].items()
        if key.startswith("global_") or key in (
            "global_metadata_comparison", "global_metadata_comparison_valid",
            "regional_api_values_match", "api_face_value_zec_diagnostic_only",
        )
    })
    output["zec_context_archive"] = update_context_archive(output, now=now)
    output["zec_predictive_engine"] = evaluate_predictive_engine(output)
    output["zec_prediction_tracking"] = update_prediction_tracking(output, now=now)
    output["zec_predictive_engine"]["context_archive"] = output["zec_context_archive"]
    write_json_atomically(OUT, output)
    print(json.dumps({
        "output": str(OUT),
        "bytes": OUT.stat().st_size,
        "schema_version": output["schema_version"],
        "source_status": output["source_status"],
        "market_records": len(market_out),
        "spot_history_ready": {
            market: {bar: item["ready_200"] for bar, item in bars.items()}
            for market, bars in spot_history.items()
        },
        "xperp_history_ready": {bar: item["ready_200"] for bar, item in xperp_history.items()},
        "funding_diagnostics": output["funding_diagnostics"]["status"],
        "book_snapshots": output["microstructure"]["book_snapshot_count"],
        "trades": output["microstructure"]["trade_count"],
        "trade_id_gap_events": output["microstructure"]["trade_id_gap_events"],
        "ws_microstructure_status": (output["microstructure"].get("websocket") or {}).get("status"),
        "ws_book_valid": (output["microstructure"].get("websocket") or {}).get("book_valid"),
        "zec_early_signal_status": output["zec_early_signal"]["status"],
        "zec_early_signal_decision": output["zec_early_signal"]["decision"],
        "zec_early_signal_blockers": output["zec_early_signal"]["data_quality"]["blockers"],
        "zec_shadow_signal_status": output["zec_shadow_signal"]["status"],
        "zec_shadow_signal_decision": output["zec_shadow_signal"]["decision"],
        "zec_shadow_candidate_direction": output["zec_shadow_signal"]["candidate_direction"],
        "zec_predictive_engine_archive": output["zec_predictive_engine"]["archive"],
        "zec_predictive_engine_scalping_status": output["zec_predictive_engine"]["scalping"]["status"],
        "zec_predictive_engine_reversals_status": output["zec_predictive_engine"]["reversals"]["status"],
        "zec_context_observations": output["zec_context_archive"]["observations_total"],
        "zec_confirmed_setups": output["zec_context_archive"]["setups_total"],
        "zec_setup_outcomes_matured": output["zec_context_archive"]["outcomes_matured"],
        "zec_movement_events": output["zec_context_archive"].get("movement_events_total", 0),
        "zec_prediction_tracking_status": output["zec_prediction_tracking"].get("status"),
        "zec_predictions_total": output["zec_prediction_tracking"].get("predictions_total"),
        "zec_prediction_outcomes_matured": output["zec_prediction_tracking"].get("mature_outcomes_total"),
    }))


if __name__ == "__main__":
    main()
