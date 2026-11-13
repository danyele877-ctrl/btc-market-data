# BTC Market Data

Free public-data collector for a Bitcoin market dashboard. No exchange credentials or paid API keys are required.

## What it collects
- BTC spot price: Binance public market-data endpoint and OKX
- BTC perpetual market data: OKX ticker, funding and open interest; Deribit BTC perpetual summary
- Derivatives context: cross-exchange funding (Edge or Not), BTC funding/open-interest series and liquidation data (SwoleCharts), plus CoinBoss funding, open interest, liquidation and ticker feeds
- US spot Bitcoin ETF daily/session net flows (TFTC open dataset)
- US macro series from FRED: 10Y Treasury yield (DGS10), broad trade-weighted USD index (DTWEXBGS), Fed funds rate (FEDFUNDS), CPI (CPIAUCSL), core CPI (CPILFESL), unemployment (UNRATE)

## Files
- `data/latest.json`: latest raw source payloads, retrieval timestamps, latency, endpoint and source status
- `data/quality.json`: freshness and payload validation, required-source failures and overall status

A source error is recorded, not silently replaced with an old value. Optional sources may fail without invalidating the entire snapshot. Required-feed failures mark the snapshot `degraded`, but the workflow still saves the diagnostic snapshot.

## Schedule
GitHub Actions runs on a 15-minute cron and after collector/validator/workflow code changes. GitHub scheduled workflows can start late; this is not a real-time feed or a guaranteed 15-minute SLA.

## Run manually
Repository → Actions → Update BTC Market Data → Run workflow.

## Data caveats
- Deribit's public BTC perpetual `open_interest` field is reported with an approximately 8-hour delay; do not treat it as live OI.
- ETF flows are daily/session data and may be incomplete or revised by the provider.
- FRED observations update on their own release calendars, not every collection cycle. The collector requests each series separately because daily and monthly series can be returned in different response formats.
- Retrieval freshness is not the same as observation freshness. Always inspect each source's timestamp and underlying observation date before interpreting it.
- Three Edge or Not endpoints for liquidations and open interest were removed from the collector after repeated timeouts in GitHub Actions. Their roles are covered by the remaining CoinBoss and SwoleCharts feeds; Edge or Not cross-exchange funding remains enabled.

All feeds are public endpoints. Do not add API keys, passwords, exchange credentials or personal data to this public repository.

## Continuous OKX candle feed

The scheduled GitHub Actions collector remains unchanged. It is still a 15-minute job and is not the always-on process. The separate `candle_service.py` process maintains a public, read-only OKX WebSocket connection and serves its own JSON feed without changing the schema of `data/latest.json`.

It subscribes to completed 1-minute and 5-minute candles for `BTC-USDC`, `ZEC-USDC`, and `ZEC-USD_UM_XPERP` when OKX's public instrument metadata reports them as live. Spot pairs that are absent or not live are listed in `unavailable_instruments` and are not subscribed. The ZEC X-Perp is the `FUTURES` instrument `ZEC-USD_UM_XPERP`; this is not `ZEC-USDT-SWAP`.

### Feed endpoints

- `GET /candles.json` (also `/`): feed state, candle arrays, connection state, UTC timestamps, and staleness metadata.
- `GET /healthz`: process and WebSocket health, each stream's latest completed candle time, age, threshold, and stale flag.

Each candle has `ts_ms`, UTC `timestamp` and `completed_at`, and string-valued OHLCV fields to avoid losing decimal precision. Open candles are ignored. The service keeps the latest 500 completed candles per instrument and interval in memory. A process restart creates a short warm-up while subscriptions resume; no historical data is fabricated.

The WebSocket client sends subscriptions after each connection, uses OKX's application-level text ping/pong heartbeat and reconnects with exponential backoff if the pong does not arrive. All timestamps are UTC. The collector follows OKX's [official WebSocket candlestick channel](https://www.okx.com/docs-v5/en/#websocket-api-public-channel) and resolves live instruments from the [public instruments metadata](https://www.okx.com/docs-v5/en/#rest-api-public-data). Only public market endpoints are used; no OKX API keys or trading permissions are used or supported.

### Run locally

```sh
python -m pip install -r requirements.txt
python candle_service.py
```

The default port is `8080`; set `PORT` to override it. Set `CANDLE_DATA_FILE` if you want a local JSON copy. Run the unit tests with `python -m unittest discover -s tests`.

### Always-on deployment option

`render.yaml` defines an opt-in Render web service using the Dockerfile and an always-on paid `starter` plan. To deploy, connect this GitHub repository to Render, select **Blueprint** deployment, review the plan, and create the service. `autoDeployTrigger` is set to `off`, so later repository changes require a manual deploy. Render provides the HTTPS service URL; append `/candles.json` for the data and `/healthz` for status.

No secret is needed for OKX. The feed endpoint contains public market data and is read-only. Keep the existing GitHub Actions workflow enabled for the BTC snapshot; the continuous feed runs independently and does not overwrite `data/latest.json`. Deployment requires the owner to connect the repository to a Render account and approve the paid always-on service.
