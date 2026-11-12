import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

TIMEOUT = 12
MAX_WORKERS = 15
USER_AGENT = "BTC-Market-Data/1.2"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT}
    )
    started = time.time()
    last_error = None

    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))

            # Bybit may return HTTP 200 with an application-level error.
            if isinstance(data, dict) and "retCode" in data and data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit retCode={data.get('retCode')}: {data.get('retMsg', '')}"
                )

            return {
                "data": data,
                "latency_ms": round((time.time() - started) * 1000),
                "attempts": attempt + 1
            }

        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)

    raise last_error


def collect(item):
    name, url = item

    try:
        result = fetch_json(url)
        return name, {
            "status": "ok",
            "retrieved_at": utc_now(),
            "latency_ms": result["latency_ms"],
            "data": result["data"]
        }

    except Exception as exc:
        return name, {
            "status": "error",
            "retrieved_at": utc_now(),
            "error": f"{type(exc).__name__}: {str(exc)}"[:300]
        }


def main():
    started = time.time()

    sources = {
        "binance_spot":
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",

        "okx_spot":
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",

        # Bybit public market data: independent fallback for spot and derivatives.
        "bybit_spot":
            "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",

        "bybit_futures_ticker":
            "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",

        "bybit_open_interest":
            "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=5min&limit=1",

        "bybit_funding_history":
            "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1",

        "binance_futures_price":
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",

        "binance_open_interest":
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",

        "okx_futures_ticker":
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",

        "okx_funding":
            "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP",

        "okx_open_interest":
            "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP",

        "edge_liquidations_24h":
            "https://edgeornot.com/api/liquidations?hours=24&coin=btc",

        "edge_btc_open_interest":
            "https://edgeornot.com/api/oi/btc?days=1",

        "edge_funding_all_exchanges":
            "https://edgeornot.com/api/funding",

        "edge_open_interest_by_exchange":
            "https://edgeornot.com/api/oi-by-exchange",

        # SwoleCharts free public API: BTC derivatives history and snapshot.
        "swole_funding_btc_1d":
            "https://swolecharts.com/api/series/funding?base=BTC&range=1d&step=1h",

        "swole_open_interest_btc_1d":
            "https://swolecharts.com/api/series/oi?base=BTC&range=1d&step=1h",

        "swole_liquidations_btc_1d":
            "https://swolecharts.com/api/series/liquidations?base=BTC&range=1d&step=1h",

        "swole_btc_snapshot":
            "https://swolecharts.com/api/coin/BTC",

        # CoinBoss public API: independently collected BTC derivatives and liquidation data.
        "coinboss_funding_btc":
            "https://www.coinboss.com/api/sd/funding?coin=BTC",

        "coinboss_open_interest_btc":
            "https://www.coinboss.com/api/sd/oi?coin=BTC",

        "coinboss_liquidations_overview":
            "https://www.coinboss.com/api/liquidations/overview",

        "coinboss_liquidations_btc":
            "https://www.coinboss.com/api/liquidations?symbol=BTCUSDT&limit=100",

        "coinboss_market_tickers_btc":
            "https://www.coinboss.com/api/market/tickers?coin=BTC",
    }

    results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(collect, item)
            for item in sources.items()
        ]

        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result

    ordered_results = {
        name: results[name]
        for name in sources
    }

    successful = [
        name for name, item in ordered_results.items()
        if item["status"] == "ok"
    ]

    failed = [
        name for name, item in ordered_results.items()
        if item["status"] != "ok"
    ]

    output = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "asset": "BTC",
        "collection_duration_ms": round(
            (time.time() - started) * 1000
        ),
        "sources": ordered_results,
        "summary": {
            "successful_sources": len(successful),
            "failed_sources": len(failed),
            "total_sources": len(sources),
            "successful_names": successful,
            "failed_names": failed
        }
    }

    os.makedirs("data", exist_ok=True)

    with open("data/latest.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(json.dumps(output["summary"], indent=2))
    print(
        f"Collection duration: "
        f"{output['collection_duration_ms']} ms"
    )


if __name__ == "__main__":
    main()
