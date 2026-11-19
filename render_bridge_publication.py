#!/usr/bin/env python3
"""Monotonic publication guard for the Render-to-GitHub radar bridge."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EXIT_PUBLISH = 0
EXIT_INVALID_CANDIDATE = 1
EXIT_STALE_CANDIDATE = 10
REQUIRED_BUNDLE_FILES = {
    "render_feed.json",
    "zec_microstructure_history.json",
    "zec_xperp_history.json",
    "radar_snapshot.json",
    "zec_prediction_tracking.json",
    "zec_predictive_archive.json",
    "zec_context_archive.json",
}
XPERP_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "1H": 3_600_000, "4H": 14_400_000}
XPERP_MAX_AGE_SECONDS = {"1m": 180, "5m": 360, "1H": 3 * 3600, "4H": 12 * 3600}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload else None


def _load_candidate_documents(
    bundle_files: list[tuple[Path, Path]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    documents: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for source, destination in bundle_files:
        name = destination.name
        if name in documents:
            problems.append(f"duplicate publication target: {name}")
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"{name} candidate is unreadable or invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict) or not payload:
            problems.append(f"{name} candidate must be a non-empty JSON object")
            continue
        documents[name] = payload
    missing = REQUIRED_BUNDLE_FILES - set(documents)
    if missing:
        problems.append("publication bundle is missing: " + ", ".join(sorted(missing)))
    return documents, problems


def _microstructure_problems(
    micro: dict[str, Any], feed: dict[str, Any], captured: datetime,
) -> list[str]:
    problems: list[str] = []
    expected_instrument = "ZEC-USD_UM_XPERP-310530"
    if micro.get("schema_version") != 1 or micro.get("instrument") != expected_instrument:
        problems.append("microstructure history schema or instrument is invalid")
    arrays = ("trades", "book_snapshots", "open_interest_history", "capture_status")
    if any(not isinstance(micro.get(key), list) for key in arrays):
        return problems + ["microstructure history collections are missing or malformed"]

    trades = micro["trades"]
    books = micro["book_snapshots"]
    oi_rows = micro["open_interest_history"]
    captures = micro["capture_status"]
    if not trades and not books and not oi_rows:
        problems.append("microstructure history contains no acquired market records")
    if not captures:
        problems.append("microstructure history has no capture provenance")

    def validate_timestamps(rows: list[Any], label: str) -> list[int]:
        timestamps: list[int] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                problems.append(f"microstructure {label}[{index}] is not an object")
                continue
            try:
                timestamp = int(row["ts_ms"])
            except (KeyError, TypeError, ValueError):
                problems.append(f"microstructure {label}[{index}] has no valid ts_ms")
                continue
            if timestamp <= 0 or timestamp > int(captured.timestamp() * 1000) + 1000:
                problems.append(f"microstructure {label}[{index}] has an invalid or future timestamp")
                continue
            timestamps.append(timestamp)
        if timestamps != sorted(timestamps):
            problems.append(f"microstructure {label} is not chronologically ordered")
        return timestamps

    validate_timestamps(trades, "trades")
    validate_timestamps(books, "book_snapshots")
    validate_timestamps(oi_rows, "open_interest_history")

    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            continue
        try:
            size = float(trade.get("sz", 0))
            price = float(trade.get("px", 0))
        except (TypeError, ValueError):
            size, price = 0, 0
        if (
            not trade.get("tradeId")
            or trade.get("source") not in ("rest", "websocket")
            or trade.get("side") not in ("buy", "sell")
            or not math.isfinite(size) or size <= 0
            or not math.isfinite(price) or price <= 0
        ):
            problems.append(f"microstructure trade[{index}] lacks valid source-backed trade fields")
            break
    for index, book in enumerate(books):
        if not isinstance(book, dict):
            continue
        if (
            book.get("source") not in ("rest", "websocket")
            or not isinstance(book.get("bids"), list) or not book["bids"]
            or not isinstance(book.get("asks"), list) or not book["asks"]
        ):
            problems.append(f"microstructure book_snapshot[{index}] lacks source-backed bid/ask levels")
            break
    for index, row in enumerate(oi_rows):
        if not isinstance(row, dict):
            continue
        if row.get("inst_id") != expected_instrument or row.get("unit") != "contracts":
            problems.append(f"microstructure open_interest_history[{index}] has invalid instrument or unit")
            break

    if captures:
        latest_capture = captures[-1]
        stamp = _timestamp(latest_capture.get("captured_at")) if isinstance(latest_capture, dict) else None
        if stamp is None or stamp > captured:
            problems.append("microstructure latest capture timestamp is missing or after the feed")

    summary = feed.get("microstructure_history")
    if not isinstance(summary, dict):
        problems.append("feed is missing microstructure history summary")
    else:
        for field, actual in (
            ("unique_trades", len(trades)),
            ("book_snapshots", len(books)),
            ("websocket_trades", sum(row.get("source") == "websocket" for row in trades if isinstance(row, dict))),
            ("websocket_book_snapshots", sum(row.get("source") == "websocket" for row in books if isinstance(row, dict))),
        ):
            if summary.get(field) != actual:
                problems.append(f"feed/microstructure generation mismatch for {field}")
    return problems


def _xperp_history_problems(
    history: dict[str, Any], feed: dict[str, Any], captured: datetime,
) -> list[str]:
    problems: list[str] = []
    instrument = "ZEC-USD_UM_XPERP-310530"
    metadata = feed.get("zec_xperp_history")
    if history.get("instrument") != instrument or not isinstance(metadata, dict) or metadata.get("instrument") != instrument:
        return ["ZEC X-Perp history instrument or feed metadata is invalid"]
    retrieved = _timestamp(history.get("retrieved_at"))
    feed_retrieved = _timestamp(metadata.get("retrieved_at"))
    if retrieved is None or retrieved != feed_retrieved or retrieved > captured:
        problems.append("ZEC X-Perp history retrieval timestamp does not match the feed generation")
    bars = history.get("bars")
    summary_bars = metadata.get("bars")
    if not isinstance(bars, dict) or not isinstance(summary_bars, dict):
        return problems + ["ZEC X-Perp history bars are missing or malformed"]
    for bar, interval_ms in XPERP_INTERVAL_MS.items():
        record = bars.get(bar)
        summary = summary_bars.get(bar)
        if not isinstance(record, dict) or not isinstance(summary, dict):
            problems.append(f"ZEC X-Perp {bar} history is missing")
            continue
        candles = record.get("candles")
        if (
            record.get("http_status") != 200 or record.get("ready_200") is not True
            or record.get("gap_count") != 0 or not isinstance(candles, list)
            or len(candles) < 200 or record.get("count") != len(candles)
        ):
            problems.append(f"ZEC X-Perp {bar} history is not a complete HTTP 200 series")
            continue
        timestamps: list[int] = []
        for index, candle in enumerate(candles):
            if not isinstance(candle, dict):
                problems.append(f"ZEC X-Perp {bar} candle[{index}] is not an object")
                break
            try:
                timestamp = int(candle["ts_ms"])
                values = [float(candle[name]) for name in ("open", "high", "low", "close")]
                completed = _timestamp(candle.get("completed_at"))
            except (KeyError, TypeError, ValueError):
                problems.append(f"ZEC X-Perp {bar} candle[{index}] is malformed")
                break
            if (
                timestamp <= 0 or timestamp % interval_ms or completed is None
                or abs(int(completed.timestamp() * 1000) - (timestamp + interval_ms)) > 1000
                or completed > captured or not all(math.isfinite(value) for value in values)
                or values[2] <= 0 or values[1] < max(values[0], values[3], values[2])
                or values[2] > min(values[0], values[3], values[1])
            ):
                problems.append(f"ZEC X-Perp {bar} candle[{index}] is invalid or incomplete")
                break
            timestamps.append(timestamp)
        if timestamps and (
            timestamps != sorted(set(timestamps))
            or any(right - left != interval_ms for left, right in zip(timestamps, timestamps[1:]))
        ):
            problems.append(f"ZEC X-Perp {bar} candles are unordered, duplicated, or gapped")
        newest = _timestamp(candles[-1].get("completed_at")) if candles and isinstance(candles[-1], dict) else None
        age = (captured - newest).total_seconds() if newest is not None else None
        if (
            summary.get("http_status") != record.get("http_status")
            or summary.get("count") != len(candles)
            or summary.get("gap_count") != record.get("gap_count")
            or summary.get("newest_completed_at") != candles[-1].get("completed_at")
            or newest is None or age is None or age < 0 or age > XPERP_MAX_AGE_SECONDS[bar]
        ):
            problems.append(f"ZEC X-Perp {bar} candles do not match feed metadata or freshness")
    return problems


def validate_candidate_bundle(documents: dict[str, dict[str, Any]]) -> list[str]:
    """Validate candidate JSON files as one source-coherent publication bundle."""
    problems: list[str] = []
    missing = REQUIRED_BUNDLE_FILES - set(documents)
    if missing:
        return ["publication bundle is missing: " + ", ".join(sorted(missing))]
    feed = documents["render_feed.json"]
    snapshot = documents["radar_snapshot.json"]
    captured = _timestamp(feed.get("captured_at"))
    if captured is None:
        return ["candidate feed captured_at is missing or invalid"]
    problems.extend(_microstructure_problems(documents["zec_microstructure_history.json"], feed, captured))
    problems.extend(_xperp_history_problems(documents["zec_xperp_history.json"], feed, captured))
    try:
        from capture_render_feed import validate_feed
        from radar_alert_contract import validate_snapshot

        problems.extend(f"candidate feed: {item}" for item in validate_feed(feed))
        problems.extend(f"candidate radar snapshot: {item}" for item in validate_snapshot(snapshot))
        safety = snapshot.get("zec_contract_safety")
        if isinstance(safety, dict):
            required_false = (
                "ctVal_reconciled", "sizing_allowed", "orders_enabled",
                "theoretical_sizing_permitted", "position_sizing_enabled",
            )
            for field in required_false:
                if safety.get(field) is not False:
                    problems.append(f"candidate radar snapshot must keep {field}=false")
            if safety.get("ctVal_official_spec_zec") != 0.01 or safety.get("ctVal_account_api_observed_zec") != 0.01:
                problems.append("candidate radar snapshot must report the verified 0.01 ZEC contract basis")
            if safety.get("ctVal_status") != "CTVAL_VERIFICATO_SIZING_BLOCCATO":
                problems.append("candidate radar snapshot must keep CTVAL verified while sizing remains blocked")
            if safety.get("ctVal_source_verified") is not True:
                problems.append("candidate radar snapshot must prove the source-verified CTVAL basis")
    except Exception as exc:
        problems.append(f"candidate contract validation could not run: {type(exc).__name__}: {exc}")
    return problems


def install_json_atomically(source: Path, destination: Path) -> None:
    """Install a validated JSON document using a same-directory atomic replace."""
    source, destination = Path(source), Path(destination)
    raw = source.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"{source} must contain a non-empty JSON object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


ALERT_MAX_AGE = timedelta(seconds=180)  # Existing early-signal capture age limit.
BTC_ID = "BTC-USDC"
ZEC_SPOT_ID = "ZEC-USDC"
ZEC_ID = "ZEC-USD_UM_XPERP-310530"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        price = float(value)
    except (ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _quote(entry: Any, instrument: str, field: str) -> tuple[float | None, datetime | None]:
    row = _object(entry)
    first = _object(row.get("first"))
    if row.get("http_status") != 200 or row.get("api_code") != "0" or row.get("error"):
        return None, None
    if first.get("instId") != instrument:
        return None, None
    try:
        observed = datetime.fromtimestamp(int(first["ts"]) / 1000, tz=timezone.utc)
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        observed = None
    return _positive_price(first.get(field)), observed


def build_compact_alert(snapshot: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Project the validated snapshot without deriving a trading direction."""
    captured = _timestamp(snapshot.get("source_captured_at"))
    generated = _timestamp(snapshot.get("generated_at"))
    market = _object(snapshot.get("market"))
    btc, btc_time = _quote(market.get("btc_spot_ticker"), BTC_ID, "last")
    spot, spot_time = _quote(market.get("zec_spot_ticker"), ZEC_SPOT_ID, "last")
    zec, zec_time = _quote(market.get("zec_xperp_ticker"), ZEC_ID, "last")
    mark, mark_time = _quote(market.get("zec_xperp_mark"), ZEC_ID, "markPx")
    observations = [captured, btc_time, spot_time, zec_time]
    if mark is not None:
        observations.append(mark_time)
    valid_times = (
        captured is not None and generated is not None and captured <= generated <= now
        and all(stamp is not None and stamp <= generated for stamp in observations)
    )
    expires = min(observations) + ALERT_MAX_AGE if valid_times else None

    early = _object(snapshot.get("zec_early_signal"))
    quality = _object(early.get("data_quality"))
    health = _object(snapshot.get("feed_health"))
    feed_ok = (
        snapshot.get("source_status") == snapshot.get("feed_capture_status") == health.get("status") == "ok"
        and snapshot.get("feed_source_mode") == health.get("source_mode")
        and snapshot.get("feed_source_mode") in ("OKX_EEA_REST_SNAPSHOT", "OKX_EEA_REST_WS_SNAPSHOT", "LEGACY_RENDER_WEBSOCKET")
    )
    inputs_ready = (
        quality.get("websocket_ready") is True
        and quality.get("required_groups_ready") is True
        and quality.get("blockers") == []
    )
    prices_available = all(price is not None for price in (btc, spot, zec))
    direction = early.get("decision")
    trigger_expiry = _timestamp(early.get("valid_until"))
    confirmed = (
        direction in ("LONG", "SHORT") and early.get("status") == "TRIGGER CONFERMATO"
        and early.get("candidate_direction") == direction
        and _timestamp(early.get("captured_at")) == captured
        and _object(early.get("instrument")).get("inst_id") == ZEC_ID
    )
    if expires is not None and inputs_ready and confirmed and trigger_expiry is not None:
        expires = min(expires, trigger_expiry)
    fresh = expires is not None and now < expires
    evidence_current = trigger_expiry is not None and now < trigger_expiry
    engine_ready = inputs_ready and feed_ok and prices_available and fresh and evidence_current
    blockers = list(quality.get("blockers") or [])
    if inputs_ready and not evidence_current:
        blockers.append("engine_evidence_expired_or_missing")
    if not feed_ok:
        blockers.append("feed_unavailable")
    if not prices_available:
        blockers.append("required_prices_missing")
    if not fresh:
        blockers.append("stale_data" if valid_times else "invalid_timestamps")
    invalidation = _object(early.get("invalidation"))
    level = _positive_price(invalidation.get("price"))
    decision = {"side": "NO_SIGNAL", "reason": "invalid_timestamps"}
    if valid_times:
        decision["reason"] = "stale_data"
        if fresh:
            decision["reason"] = "data_quality_gate"
            if engine_ready:
                decision["reason"] = "no_confirmed_trigger"
                if confirmed and trigger_expiry is not None:
                    decision["reason"] = "invalidated_or_missing_level"
                    quotes = [price for price in (zec, mark) if price is not None]
                    if level is not None and invalidation.get("direction") == direction and all(
                        price > level if direction == "LONG" else price < level for price in quotes
                    ):
                        decision = {"side": direction, "reason": "confirmed_trigger", "invalidation_price": level}

    return {
        "schema_version": 1,
        "generated_at": snapshot.get("generated_at"),
        "captured_at": snapshot.get("source_captured_at"),
        "prices": {BTC_ID: {"last": btc}, ZEC_SPOT_ID: {"last": spot}, ZEC_ID: {"last": zec, "mark": mark}},
        "feed": {
            "source": snapshot.get("feed_source_mode"),
            "status": "ok" if feed_ok else "unavailable",
            "fresh_at_publication": fresh,
            "expires_at": expires.isoformat() if expires is not None else None,
        },
        "quality": {
            "engine_ready": engine_ready,
            "required_prices_available": prices_available,
            "blockers": blockers if not engine_ready else [],
        },
        "decision": decision,
    }


def publication_decision(
    candidate_feed: dict[str, Any] | None,
    candidate_snapshot: dict[str, Any] | None,
    published_feed: dict[str, Any] | None,
    published_snapshot: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Reject malformed candidates and any candidate older than either published file."""
    if not isinstance(candidate_feed, dict) or not candidate_feed:
        return False, "candidate feed is empty or invalid"
    if not isinstance(candidate_snapshot, dict) or not candidate_snapshot:
        return False, "candidate snapshot is empty or invalid"
    if candidate_feed.get("status") != "ok" or candidate_snapshot.get("source_status") != "ok":
        return False, "candidate feed/snapshot status is not ok"

    candidate_time = _timestamp(candidate_feed.get("captured_at"))
    snapshot_source_time = _timestamp(candidate_snapshot.get("source_captured_at"))
    snapshot_generated_time = _timestamp(candidate_snapshot.get("generated_at"))
    if candidate_time is None or snapshot_source_time is None or snapshot_generated_time is None:
        return False, "candidate timestamps are missing or invalid"
    if candidate_time != snapshot_source_time:
        return False, "candidate feed and snapshot timestamps do not match"
    if snapshot_generated_time < snapshot_source_time:
        return False, "candidate snapshot predates its feed"

    published_times = []
    if isinstance(published_feed, dict):
        stamp = _timestamp(published_feed.get("captured_at"))
        if stamp is not None:
            published_times.append(stamp)
    if isinstance(published_snapshot, dict):
        for field in ("source_captured_at", "generated_at"):
            stamp = _timestamp(published_snapshot.get(field))
            if stamp is not None:
                published_times.append(stamp)

    newest_published = max(published_times, default=None)
    if newest_published is not None and candidate_time < newest_published:
        return False, "candidate is older than an already published feed or snapshot"
    return True, "candidate is current or newer than published data"


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-feed", required=True, type=Path)
    parser.add_argument("--candidate-snapshot", required=True, type=Path)
    parser.add_argument("--published-feed", required=True, type=Path)
    parser.add_argument("--published-snapshot", required=True, type=Path)
    parser.add_argument(
        "--bundle-file", action="append", nargs=2, default=[],
        metavar=("SOURCE", "DESTINATION"),
        help="candidate JSON source and published path; when supplied, all seven bridge files are validated and atomically installed",
    )
    args = parser.parse_args()

    bundle_files = [(Path(source), Path(destination)) for source, destination in args.bundle_file]
    if bundle_files:
        documents, problems = _load_candidate_documents(bundle_files)
        paths_by_name = {destination.name: source for source, destination in bundle_files}
        if paths_by_name.get("render_feed.json") != args.candidate_feed:
            problems.append("bundle render_feed.json must be the candidate feed used by the publication guard")
        if paths_by_name.get("radar_snapshot.json") != args.candidate_snapshot:
            problems.append("bundle radar_snapshot.json must be the candidate snapshot used by the publication guard")
        if not problems:
            problems.extend(validate_candidate_bundle(documents))
        if problems:
            for problem in problems:
                print(f"Publication bundle validation failed: {problem}", file=sys.stderr)
            return EXIT_INVALID_CANDIDATE

    publish, reason = publication_decision(
        _read_json(args.candidate_feed),
        _read_json(args.candidate_snapshot),
        _read_json(args.published_feed),
        _read_json(args.published_snapshot),
    )
    print(reason)
    if publish:
        try:
            if bundle_files:
                compact = build_compact_alert(documents["radar_snapshot.json"], datetime.now(timezone.utc))
                with tempfile.TemporaryDirectory() as directory:
                    candidate = Path(directory) / "radar_alert.json"
                    candidate.write_text(
                        json.dumps(compact, separators=(",", ":"), allow_nan=False) + "\n",
                        encoding="utf-8",
                    )
                    compact_destination = args.published_snapshot.with_name(candidate.name)
                    for source, destination in [*bundle_files, (candidate, compact_destination)]:
                        install_json_atomically(source, destination)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"Atomic publication install failed: {exc}", file=sys.stderr)
            return EXIT_INVALID_CANDIDATE
        if bundle_files:
            print(f"Installed {len(bundle_files) + 1} radar JSON files with atomic replacements.")
        return EXIT_PUBLISH
    if "older than" in reason:
        return EXIT_STALE_CANDIDATE
    return EXIT_INVALID_CANDIDATE


if __name__ == "__main__":
    sys.exit(_cli())
