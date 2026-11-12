import csv
import io
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

TIMEOUT = 15
MAX_WORKERS = 12
USER_AGENT = "Mozilla/5.0 (compatible; BTC-Market-Data/2.0; +https://github.com/danyele877-ctrl/btc-market-data)"

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def request_bytes(url, accept="application/json,text/csv,text/plain,*/*"):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Cache-Control": "no-cache"
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()

def fetch_json(urls):
    if isinstance(urls, str):
        urls = [urls]
    errors = []
    started = time.time()
    for url in urls:
        try:
            raw = request_bytes(url)
            data = json.loads(raw.decode("utf-8-sig"))
            if isinstance(data, dict):
                if "retCode" in data and data.get("retCode") != 0:
                    raise RuntimeError(f"API retCode={data.get('retCode')}: {data.get('retMsg', '')}")
                if data.get("code") not in (None, "0", 0):
                    raise RuntimeError(f"API code={data.get('code')}: {data.get('msg', '')}")
            return {"data": data, "latency_ms": round((time.time()-started)*1000), "endpoint": url}
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError(" | ".join(errors)[-900:])

def parse_fred_csv(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # FRED CSV is numeric/date data; latin-1 preserves every byte safely.
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        date = row.get("observation_date") or row.get("DATE")
        if not date:
            continue
        values = {}
        for key, value in row.items():
            if key in ("observation_date", "DATE") or value in (None, "", "."):
                continue
            try:
                values[key] = float(value)
            except (TypeError, ValueError):
                pass
        if values:
            rows.append({"date": date, "values": values})
    if not rows:
        raise ValueError("FRED CSV contains no numeric observations")
    latest = rows[-1]
    return {
        "provider": "FRED",
        "series": list(latest["values"].keys()),
        "latest_observation": latest,
        "observations": rows[-45:]
    }

def fetch_fred():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10,DTWEXBGS,FEDFUNDS,CPIAUCSL,CPILFESL,UNRATE"
    started = time.time()
    raw = request_bytes(url, "text/csv,text/plain,*/*")
    return {"data": parse_fred_csv(raw), "latency_ms": round((time.time()-started)*1000), "endpoint": url}

def fetch_coinboss_tickers():
    result = fetch_json("https://www.coinboss.com/api/market/tickers?coin=BTC")
    payload = result["data"]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload["data"] = [row for row in payload["data"] if row.get("coin") == "BTC"]
    result["data"] = payload
    return result

def collect(item):
    name, fetcher = item
    try:
        result = fetcher()
        return name, {"status": "ok", "retrieved_at": utc_now(),
                      "latency_ms": result["latency_ms"], "endpoint": result["endpoint"],
                      "data": result["data"]}
    except Exception as exc:
        return name, {"status": "error", "retrieved_at": utc_now(),
                      "error": f"{type(exc).__name__}: {str(exc)}"[:900]}

def main():
    started = time.time()
    sources = {
        # Binance public market-data-only domain avoids regional restrictions on api.binance.com.
        "binance_spot": lambda: fetch_json("https://data-api.binance.vision/api/v3/ticker/24hr?symbol=BTCUSDT"),
        "okx_spot": lambda: fetch_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
        # Bybit documents api.bytick.com as an alternate mainnet host.
        "bybit_spot": lambda: fetch_json(["https://api.bytick.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
                                            "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"]),
        "bybit_futures_ticker": lambda: fetch_json(["https://api.bytick.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
                                                      "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"]),
        "bybit_open_interest": lambda: fetch_json(["https://api.bytick.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=5min&limit=1",
                                                     "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=5min&limit=1"]),
        "bybit_funding_history": lambda: fetch_json(["https://api.bytick.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1",
                                                       "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1"]),
        "binance_futures_price": lambda: fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
        "binance_open_interest": lambda: fetch_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
        "okx_futures_ticker": lambda: fetch_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"),
        "okx_funding": lambda: fetch_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"),
        "okx_open_interest": lambda: fetch_json("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP"),
        "edge_liquidations_24h": lambda: fetch_json("https://edgeornot.com/api/liquidations?hours=24&coin=btc"),
        "edge_btc_open_interest": lambda: fetch_json("https://edgeornot.com/api/oi/btc?days=1"),
        "edge_funding_all_exchanges": lambda: fetch_json("https://edgeornot.com/api/funding"),
        "edge_open_interest_by_exchange": lambda: fetch_json("https://edgeornot.com/api/oi-by-exchange"),
        "swole_funding_btc_1d": lambda: fetch_json("https://swolecharts.com/api/series/funding?base=BTC&range=1d&step=1h"),
        "swole_open_interest_btc_1d": lambda: fetch_json("https://swolecharts.com/api/series/oi?base=BTC&range=1d&step=1h"),
        "swole_liquidations_btc_1d": lambda: fetch_json("https://swolecharts.com/api/series/liquidations?base=BTC&range=1d&step=1h"),
        "swole_btc_snapshot": lambda: fetch_json("https://swolecharts.com/api/coin/BTC"),
        "coinboss_funding_btc": lambda: fetch_json("https://www.coinboss.com/api/sd/funding?coin=BTC"),
        "coinboss_open_interest_btc": lambda: fetch_json("https://www.coinboss.com/api/sd/oi?coin=BTC"),
        "coinboss_liquidations_overview": lambda: fetch_json("https://www.coinboss.com/api/liquidations/overview"),
        "coinboss_liquidations_btc": lambda: fetch_json("https://www.coinboss.com/api/liquidations?symbol=BTCUSDT&limit=100"),
        "coinboss_market_tickers_btc": fetch_coinboss_tickers,
        # TFTC publishes a free open JSON ETF-flow dataset; FRED CSV requires no API key.
        "bitcoin_etf_flows_tftc": lambda: fetch_json("https://www.tftc.io/bitcoin-etf-flows/latest.json"),
        "fred_macro": fetch_fred,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(collect, item) for item in sources.items()]
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result
    ordered = {name: results[name] for name in sources}
    ok = [n for n, v in ordered.items() if v["status"] == "ok"]
    failed = [n for n, v in ordered.items() if v["status"] != "ok"]
    output = {
        "schema_version": 3, "generated_at": utc_now(), "asset": "BTC",
        "collection_duration_ms": round((time.time()-started)*1000),
        "sources": ordered,
        "summary": {"successful_sources": len(ok), "failed_sources": len(failed),
                    "total_sources": len(sources), "successful_names": ok, "failed_names": failed}
    }
    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(output["summary"], indent=2))
    print(f"Collection duration: {output['collection_duration_ms']} ms")

if __name__ == "__main__":
    main()
