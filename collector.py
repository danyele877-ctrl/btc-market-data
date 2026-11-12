
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

TIMEOUT = 12
USER_AGENT = "BTC-Market-Data/1.0"

def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT}
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {
        "data": data,
        "latency_ms": round((time.time() - started) * 1000)
    }

def collect(name, url):
    try:
        result = fetch_json(url)
        return {
            "status": "ok",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": result["latency_ms"],
            "data": result["data"]
        }
    except Exception as exc:
        return {
            "status": "error",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc)[:300]
        }

def main():
    sources = {
        "binance_spot": (
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        ),
        "okx_spot": (
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"
        ),
        "binance_futures_price": (
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
        ),
        "binance_open_interest": (
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
        ),
        "okx_futures_ticker": (
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"
        ),
        "okx_funding": (
            "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
        ),
        "okx_open_interest": (
            "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP"
        )
    }

    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": "BTC",
        "sources": {}
    }

    for name, url in sources.items():
        output["sources"][name] = collect(name, url)

    output["summary"] = {
        "successful_sources": sum(
            1 for item in output["sources"].values()
            if item["status"] == "ok"
        ),
        "total_sources": len(sources)
    }

    import os
    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(json.dumps(output["summary"], indent=2))

if __name__ == "__main__":
    main()
