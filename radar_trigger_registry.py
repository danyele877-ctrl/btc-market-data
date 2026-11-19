#!/usr/bin/env python3
"""Append-only diagnostic registry of validated compact radar triggers.

Never enables orders, sizing, or a trading recommendation from expired data.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

INSTRUMENT = "ZEC-USD_UM_XPERP-310530"


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def record_trigger(compact: dict, archive_path: Path, now: datetime | None = None) -> bool:
    """Record only a fresh, confirmed trigger; return True if newly inserted."""
    from read_current_alert import validated_side, snapshot_is_current

    now = now or datetime.now(timezone.utc)
    if not isinstance(compact, dict) or not snapshot_is_current(compact, now):
        return False
    side = validated_side(compact, now)
    if side not in ("LONG", "SHORT"):
        return False
    captured = _time(compact.get("captured_at"))
    generated = _time(compact.get("generated_at"))
    expires = _time(compact.get("feed", {}).get("expires_at"))
    if not all((captured, generated, expires)):
        return False
    level = compact["decision"]["invalidation_price"]
    identity = json.dumps([INSTRUMENT, captured.isoformat(), generated.isoformat(),
                           side, level], separators=(",", ":"))
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    archive_path = Path(archive_path)
    archive = {"schema_version": 1, "instrument": INSTRUMENT, "events": []}
    if archive_path.exists():
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        if (archive.get("schema_version") != 1 or archive.get("instrument") != INSTRUMENT
                or not isinstance(archive.get("events"), list)):
            raise ValueError("Invalid existing trigger registry; refusing overwrite")
    if any(event.get("id") == event_id for event in archive["events"]):
        return False
    archive["events"].append({
        "id": event_id, "instrument": INSTRUMENT, "side": side,
        "captured_at": captured.isoformat(), "generated_at": generated.isoformat(),
        "expires_at": expires.isoformat(), "invalidation_price": level,
        "recorded_at": now.isoformat(), "status": "HISTORICAL_DIAGNOSTIC",
        "delivery_status": "UNKNOWN", "outcome_status": "NOT_EVALUATED",
        "orders_enabled": False, "position_sizing_enabled": False,
    })
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=archive_path.parent, prefix=".trigger.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(archive, handle, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, archive_path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True
