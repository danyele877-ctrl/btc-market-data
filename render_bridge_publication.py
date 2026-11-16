#!/usr/bin/env python3
"""Monotonic publication guard for the Render-to-GitHub radar bridge."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_PUBLISH = 0
EXIT_INVALID_CANDIDATE = 1
EXIT_STALE_CANDIDATE = 10


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload else None


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
    args = parser.parse_args()

    publish, reason = publication_decision(
        _read_json(args.candidate_feed),
        _read_json(args.candidate_snapshot),
        _read_json(args.published_feed),
        _read_json(args.published_snapshot),
    )
    print(reason)
    if publish:
        return EXIT_PUBLISH
    if "older than" in reason:
        return EXIT_STALE_CANDIDATE
    return EXIT_INVALID_CANDIDATE


if __name__ == "__main__":
    sys.exit(_cli())
