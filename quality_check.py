import json
import os
from datetime import datetime, timezone

INPUT = "data/latest.json"
OUTPUT = "data/quality.json"
MAX_AGE_SECONDS = 20 * 60

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def source_check(sources, name, required=False):
    item = sources.get(name, {})
    status = item.get("status")
    retrieved = parse_time(item.get("retrieved_at"))
    age = (datetime.now(timezone.utc) - retrieved).total_seconds() if retrieved else None
    ok = status == "ok" and age is not None and -60 <= age <= MAX_AGE_SECONDS
    data = item.get("data")
    if ok and name.startswith("coinboss_"):
        ok = isinstance(data, dict) and data.get("success") is True
    if ok and name.startswith("swole_"):
        ok = isinstance(data, dict) and bool(data.get("t")) and bool(data.get("v") or data.get("long"))
    if ok and name == "okx_spot":
        ok = isinstance(data, dict) and data.get("code") == "0" and bool(data.get("data"))
    if ok and name in ("okx_futures_ticker", "okx_funding", "okx_open_interest"):
        ok = isinstance(data, dict) and data.get("code") == "0" and bool(data.get("data"))
    return {
        "status": "ok" if ok else "warning",
        "required": required,
        "age_seconds": round(age) if age is not None else None,
        "reason": None if ok else ("source_error" if status != "ok" else "stale_or_invalid_payload")
    }

def main():
    with open(INPUT, encoding="utf-8") as f:
        snapshot = json.load(f)

    sources = snapshot.get("sources", {})
    checks = {}
    required = [
        "okx_spot", "okx_futures_ticker", "okx_funding", "okx_open_interest",
        "coinboss_funding_btc", "coinboss_open_interest_btc",
        "coinboss_market_tickers_btc", "swole_funding_btc_1d",
        "swole_open_interest_btc_1d", "swole_liquidations_btc_1d"
    ]
    for name in sources:
        checks[name] = source_check(sources, name, name in required)

    required_failed = [name for name in required if checks.get(name, {}).get("status") != "ok"]
    ok_count = sum(1 for item in checks.values() if item["status"] == "ok")

    quality = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_age_seconds": round((datetime.now(timezone.utc) - (parse_time(snapshot.get("generated_at")) or datetime.now(timezone.utc))).total_seconds()),
        "overall_status": "ok" if not required_failed else "degraded",
        "successful_sources": ok_count,
        "total_sources": len(checks),
        "required_sources": required,
        "required_failed": required_failed,
        "checks": checks
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(quality, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "overall_status": quality["overall_status"],
        "successful_sources": ok_count,
        "total_sources": len(checks),
        "required_failed": required_failed
    }, indent=2))

if __name__ == "__main__":
    main()
