#!/usr/bin/env python3
"""Validate the published Radar snapshot before it is treated as alert-ready.

This is a read-only contract check. It never calculates position size or places orders.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_SECTIONS = (
    "zec_early_signal",
    "zec_shadow_signal",
    "zec_contract_safety",
    "zec_prediction_tracking",
    "zec_predictive_engine",
)


def _is_false(value: Any) -> bool:
    return value is False


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot root must be a JSON object"]

    if not isinstance(snapshot.get("generated_at"), str) or not snapshot["generated_at"]:
        errors.append("generated_at is missing")
    if not isinstance(snapshot.get("source_status"), str) or not snapshot["source_status"]:
        errors.append("source_status is missing")

    for key in REQUIRED_SECTIONS:
        if not isinstance(snapshot.get(key), dict):
            errors.append(f"required alert section is missing or invalid: {key}")

    if errors:
        return errors

    baseline = snapshot["zec_early_signal"]
    shadow = snapshot["zec_shadow_signal"]
    safety = snapshot["zec_contract_safety"]
    tracking = snapshot["zec_prediction_tracking"]
    predictive = snapshot["zec_predictive_engine"]

    if baseline.get("mode") != "simulation_only":
        errors.append("BASELINE must remain simulation_only")
    if not isinstance(baseline.get("decision"), str) or not baseline["decision"]:
        errors.append("BASELINE decision is missing")
    if not isinstance(baseline.get("candidate_direction"), str):
        errors.append("BASELINE candidate_direction is missing")
    if not isinstance(baseline.get("confidence"), dict):
        errors.append("BASELINE confidence metadata is missing")
    elif baseline["confidence"].get("calibrated") is not False:
        errors.append("BASELINE confidence must not be presented as calibrated")

    if shadow.get("mode") != "diagnostic_only":
        errors.append("SHADOW must remain diagnostic_only")
    if shadow.get("baseline_unchanged") is not True:
        errors.append("SHADOW must explicitly confirm BASELINE is unchanged")
    if shadow.get("validation_status") in (None, ""):
        errors.append("SHADOW validation status is missing")

    for label, section in (("contract safety", safety),
                           ("BASELINE contract safety", baseline.get("contract_safety", {}))):
        if not isinstance(section, dict):
            errors.append(f"{label} section is invalid")
            continue
        for field in ("orders_enabled", "position_sizing_enabled", "sizing_allowed"):
            if field in section and not _is_false(section[field]):
                errors.append(f"{label}: {field} must be false")
        if section.get("ctVal_reconciled") is False:
            contract_value = section.get("ctVal_zec_per_contract")
            if contract_value is not None and not (
                section.get("ctVal_source_verified") is True
                and contract_value == 0.01
            ):
                errors.append(f"{label}: unreconciled CTVAL may expose only the verified 0.01 diagnostic basis")
            if section.get("theoretical_sizing_permitted") is not False:
                errors.append(f"{label}: theoretical_sizing_permitted must remain false")
            if section.get("margin_or_exposure_calculated") is not False:
                errors.append(f"{label}: margin/exposure must remain disabled while CTVAL is unreconciled")
        risk = section.get("max_risk_usdc")
        if not isinstance(risk, (int, float)) or isinstance(risk, bool) or risk <= 0:
            errors.append(f"{label}: max_risk_usdc must be a positive configured value")

    if safety.get("ctVal_reconciled") is False:
        if safety.get("ctVal_zec_per_contract") is not None and not (
            safety.get("ctVal_source_verified") is True
            and safety.get("ctVal_zec_per_contract") == 0.01
        ):
            errors.append("unreconciled CTVAL may expose only the verified 0.01 diagnostic basis")
        if safety.get("margin_or_exposure_calculated") is not False:
            errors.append("margin/exposure must remain disabled while CTVAL is unresolved")

    for label, section in (("SHADOW", shadow), ("tracking", tracking),
                           ("predictive engine", predictive)):
        for field in ("orders_enabled", "position_sizing_enabled"):
            if field in section and not _is_false(section[field]):
                errors.append(f"{label}: {field} must be false")

    execution = baseline.get("execution", {})
    if not isinstance(execution, dict):
        errors.append("BASELINE execution safety block is invalid")
    else:
        for field in ("orders_enabled", "sizing_calculated"):
            if field in execution and not _is_false(execution[field]):
                errors.append(f"BASELINE execution: {field} must be false")
        for field in ("position_quantity", "margin", "pnl"):
            if execution.get(field) is not None:
                errors.append(f"BASELINE execution: {field} must remain null in diagnostic mode")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", default="data/radar_snapshot.json")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ALERT CONTRACT FAIL: cannot read snapshot: {exc}")
        return 1

    errors = validate_snapshot(payload)
    if errors:
        print("ALERT CONTRACT FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("ALERT CONTRACT OK")
    print("Required sections: BASELINE, SHADOW, CTVAL safety, prediction tracking, predictive engine")
    print("Safety: orders disabled; position sizing disabled; uncalibrated scores remain diagnostic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
