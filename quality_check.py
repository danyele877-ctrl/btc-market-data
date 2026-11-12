import json
import os
from datetime import datetime, timezone

INPUT = "data/latest.json"
OUTPUT = "data/quality.json"
MAX_AGE_SECONDS = 25 * 60

def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

def valid_source_payload(name, data):
    if data is None:
        return False
    if name.startswith("okx_"):
        return isinstance(data, dict) and data.get("code") == "0" and bool(data.get("data"))
    if name.startswith("bybit_"):
        return isinstance(data, dict) and data.get("retCode") == 0 and bool((data.get("result") or {}).get("list"))
    if name.startswith("coinboss_"):
        return isinstance(data, dict) and data.get("success") is True
    if name.startswith("swole_") and name != "swole_btc_snapshot":
        return isinstance(data, dict) and bool(data.get("t")) and bool(data.get("v") or data.get("long"))
    if name == "fred_macro":
        latest = data.get("latest_observation", {}) if isinstance(data, dict) else {}
        return data.get("provider") == "FRED" and bool(latest.get("date")) and bool(latest.get("values"))
    if name == "bitcoin_etf_flows_tftc":
        # Provider schema can evolve; require a non-empty JSON payload and preserve it raw.
        return isinstance(data, (dict, list)) and bool(data)
    if name == "binance_spot":
        return isinstance(data, dict) and data.get("symbol") == "BTCUSDT" and bool(data.get("lastPrice") or data.get("price"))
    if name == "binance_futures_price":
        return isinstance(data, dict) and bool(data.get("symbol")) and bool(data.get("markPrice"))
    if name == "binance_open_interest":
        return isinstance(data, dict) and data.get("symbol") == "BTCUSDT" and bool(data.get("openInterest"))
    if name == "bybit_spot" or name == "bybit_futures_ticker":
        return isinstance(data, dict) and data.get("retCode") == 0 and bool((data.get("result") or {}).get("list"))
    if name in ("bybit_open_interest", "bybit_funding_history"):
        return isinstance(data, dict) and data.get("retCode") == 0 and bool((data.get("result") or {}).get("list"))
    return isinstance(data, (dict, list)) and bool(data)

def source_check(sources, name, required=False):
    item = sources.get(name, {})
    retrieved = parse_time(item.get("retrieved_at"))
    now = datetime.now(timezone.utc)
    age = (now - retrieved).total_seconds() if retrieved else None
    fresh = age is not None and -60 <= age <= MAX_AGE_SECONDS
    payload_ok = item.get("status") == "ok" and valid_source_payload(name, item.get("data"))
    ok = fresh and payload_ok
    if not fresh:
        reason = "missing_timestamp" if age is None else "stale_or_future_timestamp"
    elif item.get("status") != "ok":
        reason = "source_error"
    elif not payload_ok:
        reason = "invalid_payload"
    else:
        reason = None
    return {"status": "ok" if ok else "warning", "required": required,
            "age_seconds": round(age) if age is not None else None, "reason": reason}

def main():
    with open(INPUT, encoding="utf-8") as f:
        snapshot = json.load(f)
    sources = snapshot.get("sources", {})
    required = [
        "okx_spot", "okx_futures_ticker", "okx_funding", "okx_open_interest",
        "coinboss_funding_btc", "coinboss_open_interest_btc",
        "coinboss_market_tickers_btc", "swole_funding_btc_1d",
        "swole_open_interest_btc_1d", "swole_liquidations_btc_1d",
        "bitcoin_etf_flows_tftc"
    ]
    checks = {name: source_check(sources, name, name in required) for name in sources}
    missing = [name for name in required if name not in sources]
    required_failed = [name for name in required if checks.get(name, {}).get("status") != "ok"]
    required_failed.extend(missing)
    now = datetime.now(timezone.utc)
    snapshot_time = parse_time(snapshot.get("generated_at"))
    snapshot_age = round((now - snapshot_time).total_seconds()) if snapshot_time else None
    ok_count = sum(1 for item in checks.values() if item["status"] == "ok")
    quality = {
        "generated_at": now.isoformat(),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_age_seconds": snapshot_age,
        "overall_status": "ok" if not required_failed and snapshot_age is not None and -60 <= snapshot_age <= MAX_AGE_SECONDS else "degraded",
        "successful_sources": ok_count,
        "total_sources": len(sources),
        "required_sources": required,
        "required_failed": sorted(set(required_failed)),
        "checks": checks
    }
    if quality["overall_status"] == "degraded" and not quality["required_failed"]:
        quality["required_failed"].append("snapshot_freshness")
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(quality, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: quality[k] for k in ("overall_status", "successful_sources", "total_sources", "required_failed")}, indent=2))

if __name__ == "__main__":
    main()
