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

FRED_SERIES = ["DGS10", "DTWEXBGS", "FEDFUNDS", "CPIAUCSL", "CPILFESL", "UNRATE"]

def parse_fred_csv(raw, expected_series):
    # fredgraph.csv can return a ZIP when daily and monthly series are mixed.
    # Requesting one series per URL guarantees a plain CSV payload.
    if raw[:2] == b"PK":
        raise ValueError(f"FRED returned ZIP instead of CSV for {expected_series}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or len(rows[0]) < 2:
        preview = repr(text[:160])
        raise ValueError(f"FRED invalid CSV for {expected_series}: {preview}")

    header = [cell.strip() for cell in rows[0]]
    date_col = 0
    value_col = 1
    series_name = header[value_col] or expected_series
    observations = []
    for row in rows[1:]:
        if len(row) <= value_col:
            continue
        date = row[date_col].strip()
        value = row[value_col].strip()
        if not date or value in ("", ".", "NA", "NaN"):
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        observations.append({"date": date, "value": number})

    if not observations:
        raise ValueError(
            f"FRED CSV for {expected_series} has no numeric rows; "
            f"header={header!r}, first_data={rows[1:3]!r}"
        )
    return series_name, observations

def fetch_fred():
    started = time.time()

    def fetch_one(series):
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
        raw = request_bytes(url, "text/csv,text/plain,*/*")
        name, observations = parse_fred_csv(raw, series)
        return name, observations, url

    with ThreadPoolExecutor(max_workers=len(FRED_SERIES)) as executor:
        results = list(executor.map(fetch_one, FRED_SERIES))

    by_date = {}
    series_names = []
    latest_by_series = {}
    for name, observations, _url in results:
        series_names.append(name)
        latest_by_series[name] = observations[-1]
        for item in observations[-45:]:
            by_date.setdefault(item["date"], {})[name] = item["value"]

    merged = [
        {"date": date, "values": values}
        for date, values in sorted(by_date.items())
    ]
    latest_date = max(item["date"] for item in merged)
    latest_values = next(item["values"] for item in merged if item["date"] == latest_date)
    payload = {
        "provider": "FRED",
        "series": series_names,
        "latest_observation": {"date": latest_date, "values": latest_values},
        "latest_by_series": latest_by_series,
        "observations": merged[-45:]
    }
    return {
        "data": payload,
        "latency_ms": round((time.time()-started)*1000),
        "endpoint": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=<one-series-per-request>"
    }

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
        "okx_futures_ticker": lambda: fetch_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"),
        "okx_funding": lambda: fetch_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"),
        "okx_open_interest": lambda: fetch_json("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP"),
        # Deribit public BTC perpetual summary; its OI field is reported with an 8-hour delay.
        "deribit_btc_perpetual": lambda: fetch_json("https://www.deribit.com/api/v2/public/get_book_summary_by_instrument?instrument_name=BTC-PERPETUAL"),
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
