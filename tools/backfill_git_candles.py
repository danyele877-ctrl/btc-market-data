#!/usr/bin/env python3
"""Rebuild BTC/ZEC candle history from prior Git versions without touching source archives.

Run from a full Git clone:
  python tools/backfill_git_candles.py
  python tools/backfill_git_candles.py --since 2026-08-01 --out data/derived/git_candle_history.json

The command is read-only with respect to tracked source files. It writes only the
explicit derived output path and a sibling manifest. No radar/account/sizing code
is imported or modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_PATHS = (
    "data/radar_snapshot.json",
    "data/render_feed.json",
    "data/zec_xperp_history.json",
)
# Explicit aliases only: do not infer BTC spot from an unrelated market field.
HISTORY_ALIASES = {
    "xperp_1m": ("ZEC-USD_UM_XPERP", "1m"),
    "xperp_5m": ("ZEC-USD_UM_XPERP", "5m"),
    "xperp_15m": ("ZEC-USD_UM_XPERP", "15m"),
    "xperp_1H": ("ZEC-USD_UM_XPERP", "1H"),
    "xperp_4H": ("ZEC-USD_UM_XPERP", "4H"),
    "btc_1m": ("BTC-USDC", "1m"),
    "btc_5m": ("BTC-USDC", "5m"),
    "btc_15m": ("BTC-USDC", "15m"),
    "btc_1H": ("BTC-USDC", "1H"),
    "zec_1m": ("ZEC-USDC", "1m"),
    "zec_5m": ("ZEC-USDC", "5m"),
    "zec_15m": ("ZEC-USDC", "15m"),
    "zec_1H": ("ZEC-USDC", "1H"),
}
SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}
OHLC = ("open", "high", "low", "close")


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def parse_time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            dt = datetime.fromtimestamp(stamp, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def num(value: Any) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def validate_candle(row: Any, instrument: str, timeframe: str,
                    source: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict) or timeframe not in SECONDS:
        return None
    ts = num(row.get("ts_ms"))
    if ts is None:
        ts = num(row.get("timestamp_ms"))
    if ts is None:
        stamp = parse_time(row.get("timestamp") or row.get("time"))
        if stamp:
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
    vals = {k: num(row.get(k)) for k in OHLC}
    if ts is None or any(v is None for v in vals.values()):
        return None
    ts = int(ts)
    interval_ms = SECONDS[timeframe] * 1000
    if ts <= 0 or ts % interval_ms:
        return None
    if vals["low"] <= 0 or vals["high"] < max(vals["open"], vals["close"], vals["low"]):
        return None
    if vals["low"] > min(vals["open"], vals["close"], vals["high"]):
        return None
    completed = parse_time(row.get("completed_at"))
    expected = datetime.fromtimestamp((ts + interval_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    # If a completion timestamp exists, it must match the candle boundary.
    if completed and completed != expected:
        return None
    normalized = {
        "instrument": instrument, "timeframe": timeframe, "ts_ms": ts,
        "timestamp": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": expected,
        **{k: vals[k] for k in OHLC},
    }
    for k in ("volume", "volume_currency", "volume_quote"):
        v = num(row.get(k))
        if v is not None:
            normalized[k] = v
    normalized["provenance"] = [source]
    normalized["_value_hash"] = hashlib.sha256(
        json.dumps({k: normalized.get(k) for k in ("open", "high", "low", "close", "volume", "volume_currency", "volume_quote")},
                   sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return normalized


def emit_list(rows: Any, instrument: str, timeframe: str, source: dict[str, Any], sink: list) -> None:
    if isinstance(rows, list):
        for row in rows:
            candle = validate_candle(row, instrument, timeframe, source)
            if candle:
                sink.append(candle)


def walk_snapshot(doc: dict[str, Any], source: dict[str, Any], sink: list) -> None:
    history = doc.get("predictive_history")
    if isinstance(history, dict):
        for alias, (instrument, timeframe) in HISTORY_ALIASES.items():
            emit_list(history.get(alias), instrument, timeframe, source, sink)

    # Older/current feed schemas keep a short tail under instrument -> timeframe -> tail.
    for root_name in ("candles_1m_5m", "spot_history", "zec_xperp_history", "okx_history", "candles"):
        root = doc.get(root_name)
        if not isinstance(root, dict):
            continue
        if root_name == "zec_xperp_history" and isinstance(root.get("bars"), dict):
            root = {"ZEC-USD_UM_XPERP": root["bars"]}
        elif root_name == "candles":
            # Render bridge schema: candles.payload.instruments -> instrument -> timeframe -> rows.
            root = ((root.get("payload") or {}).get("instruments") or {})
        for instrument, frames in root.items():
            if not isinstance(frames, dict):
                continue
            canonical = str(instrument)
            if canonical in ("BTC", "BTC-USDC", "BTC-USDT"):
                canonical = "BTC-USDC"
            elif canonical in ("ZEC", "ZEC-USDC", "ZEC-USDT"):
                canonical = "ZEC-USDC"
            elif "XPERP" in canonical or canonical in ("ZEC-USD-SWAP",):
                canonical = "ZEC-USD_UM_XPERP"
            else:
                continue
            for timeframe, payload in frames.items():
                if timeframe not in SECONDS:
                    continue
                if isinstance(payload, list):
                    rows = payload
                elif isinstance(payload, dict):
                    rows = payload.get("tail") or payload.get("candles") or payload.get("rows") or payload.get("data")
                else:
                    rows = None
                emit_list(rows, canonical, timeframe, source, sink)


def commits_for(path: str, since: str | None) -> list[str]:
    args = ["log", "--all", "--format=%H", "--", path]
    if since:
        args.insert(1, f"--since={since}")
    return [x for x in git(*args).splitlines() if x]


def blob_at(commit: str, path: str) -> str | None:
    p = subprocess.run(["git", "rev-parse", f"{commit}:{path}"], text=True, capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="optional lower bound for commit date, e.g. 2026-08-01")
    parser.add_argument("--out", type=Path, default=Path("data/derived/git_candle_history.json"))
    parser.add_argument("--max-versions", type=int, default=0, help="0 means all distinct source blobs")
    args = parser.parse_args()

    if not git("rev-parse", "--is-inside-work-tree") == "true":
        raise SystemExit("Run this command inside a Git working tree.")
    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    if branch == "main":
        raise SystemExit("Refusing to run on main. Switch to research/historical-backfill-2026-11.")
    if not branch.startswith("research/"):
        raise SystemExit(f"Refusing branch '{branch}'; use an isolated research/* branch.")

    seen_blobs: set[tuple[str, str]] = set()
    candles: list[dict[str, Any]] = []
    version_count = defaultdict(int)
    commit_count = defaultdict(int)
    parse_errors = []
    for path in SOURCE_PATHS:
        commits = commits_for(path, args.since)
        commit_count[path] = len(commits)
        for commit in commits:
            blob = blob_at(commit, path)
            if not blob or (path, blob) in seen_blobs:
                continue
            seen_blobs.add((path, blob))
            version_count[path] += 1
            if args.max_versions and version_count[path] > args.max_versions:
                continue
            raw = git("cat-file", "blob", blob)
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError as exc:
                parse_errors.append({"path": path, "commit": commit, "blob": blob, "error": str(exc)})
                continue
            commit_time = git("show", "-s", "--format=%cI", commit)
            source = {
                "path": path, "commit": commit, "blob_sha": blob,
                "commit_time": parse_time(commit_time),
                "snapshot_time": parse_time(doc.get("source_captured_at") or doc.get("generated_at")
                                            or doc.get("updated_at") or doc.get("retrieved_at")),
            }
            before = len(candles)
            if isinstance(doc, dict):
                walk_snapshot(doc, source, candles)
            # Also support archives that are themselves a list of OHLC rows.
            if path.endswith("zec_xperp_history.json") and isinstance(doc, list):
                emit_list(doc, "ZEC-USD_UM_XPERP", "1m", source, candles)

    grouped: dict[tuple[str, str, int], dict[str, Any]] = {}
    conflicts = []
    for candle in candles:
        key = (candle["instrument"], candle["timeframe"], candle["ts_ms"])
        current = grouped.get(key)
        if current is None:
            grouped[key] = candle
            continue
        current["provenance"].extend(candle["provenance"])
        if current["_value_hash"] != candle["_value_hash"]:
            conflicts.append({
                "instrument": key[0], "timeframe": key[1], "ts_ms": key[2],
                "existing_hash": current["_value_hash"], "candidate_hash": candle["_value_hash"],
                "existing_provenance": current["provenance"][-2],
                "candidate_provenance": candle["provenance"][-1],
            })
            # Deterministic canonical choice: latest snapshot timestamp, then commit time.
            old_src = current["provenance"][-2]
            new_src = candle["provenance"][-1]
            if (new_src.get("snapshot_time") or new_src.get("commit_time") or "") > (
                old_src.get("snapshot_time") or old_src.get("commit_time") or ""
            ):
                keep_prov = current["provenance"]
                grouped[key] = candle
                grouped[key]["provenance"] = keep_prov
    result = []
    for key in sorted(grouped):
        row = grouped[key]
        row.pop("_value_hash", None)
        row["provenance_count"] = len(row["provenance"])
        result.append(row)

    by_series = defaultdict(list)
    for row in result:
        by_series[f'{row["instrument"]}|{row["timeframe"]}'].append(row)
    series_report = {}
    for name, rows in sorted(by_series.items()):
        seconds = SECONDS[rows[0]["timeframe"]]
        gaps = sum(1 for a, b in zip(rows, rows[1:]) if b["ts_ms"] - a["ts_ms"] != seconds * 1000)
        series_report[name] = {
            "bars": len(rows), "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
            "gaps": gaps, "duplicate_keys_after_merge": 0,
        }

    manifest = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_head": head, "git_branch": branch, "since": args.since,
        "method": "unique historical source blobs; closed-candle OHLC validation; merge by instrument/timeframe/ts_ms; conflicting variants retained in conflict report",
        "source_paths": list(SOURCE_PATHS), "commits_examined": dict(commit_count),
        "distinct_blobs_examined": dict(version_count), "valid_candle_observations": len(candles),
        "unique_candles": len(result), "conflicting_candle_keys": len(conflicts),
        "parse_errors": parse_errors, "series": series_report,
        "safety": {"orders_enabled": False, "sizing_enabled": False, "radar_modified": False,
                   "source_archives_overwritten": False, "live_execution": False},
    }
    payload = {"schema_version": 1, "generated_at": manifest["generated_at"],
               "git_head": head, "candles": result, "conflicts": conflicts}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_path = args.out.with_name(args.out.stem + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BACKFILL_ABORTED: {exc}", file=sys.stderr)
        raise
