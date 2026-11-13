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

## BTC + ZEC OKX Europe radar extension

The collector now records independent, timestamped public EEA REST snapshots for:
- BTC-USDC and ZEC-USDC spot tickers
- Exact ZEC X-Perp ticker, L2 book, recent trades, mark price, ZEC index references, funding, open interest and instrument metadata
- The exact futures ID is pinned to `ZEC-USD_UM_XPERP-310530`; no USDT swap or alternate contract is substituted.

The continuous candle service now bootstraps at least 200 completed 1H and 4H candles for that exact ZEC X-Perp using paginated requests (100 rows per OKX history page) before subscribing to the 1m/5m WebSocket channels. It refuses to mark history ready unless at least 200 valid completed candles exist on each requested timeframe. The feed keeps the stable family key `ZEC-USD_UM_XPERP` separate from the dated exchange `instId`.

Important limitations:
- The GitHub Actions collector remains a 15-minute snapshot, not a live trading feed.
- The Render candle service uses WebSocket for live 1m/5m candles; code changes are not deployed automatically because the Render blueprint has auto-deploy disabled.
- Funding interval and contract-value discrepancies are deliberately not auto-resolved by code. The raw current API metadata and timestamps are preserved for manual reconciliation.
- A successful REST request is not proof that a field is semantically comparable; inspect each endpoint's instrument ID, timestamp, venue and payload.
- Public API access only; no credentials, account access, orders or trading actions.

## Continuous OKX candle feed

The scheduled GitHub Actions collector remains unchanged. It is still a 15-minute job and is not the always-on process. The separate `candle_service.py` process maintains a public, read-only OKX WebSocket connection and serves its own JSON feed without changing the schema of `data/latest.json`.

It subscribes to completed 1-minute and 5-minute candles for `BTC-USDC`, `ZEC-USDC`, and the `ZEC-USD_UM_XPERP` futures family when OKX's public instrument metadata reports them as live. Spot pairs that are absent or not live are listed in `unavailable_instruments` and are not subscribed. The EEA catalog may list that X-Perp family under a dated exchange instrument ID (currently `ZEC-USD_UM_XPERP-310530`); discovery resolves it by `instFamily` and keeps the stable `ZEC-USD_UM_XPERP` candle key. It does not substitute `ZEC-USDT-SWAP`.

### Feed endpoints

- `GET /candles.json` (also `/`): feed state, candle arrays, connection state, UTC timestamps, and staleness metadata.
- `GET /healthz`: process and WebSocket health, each stream's latest completed candle time, age, threshold, and stale flag.

Each candle has `ts_ms`, UTC `timestamp` and `completed_at`, and string-valued OHLCV fields to avoid losing decimal precision. Open candles are ignored. The service keeps the latest 500 completed candles per instrument and interval in memory. A process restart creates a short warm-up while subscriptions resume; no historical data is fabricated.

For OKX Europe accounts, the service uses the EEA API hosts (eea.okx.com for REST and wseea.okx.com for WebSocket). The WebSocket client subscribes only to the live `candle1m` and `candle5m` channels (1H/4H are REST history intervals, not WebSocket channels), sends subscriptions after each connection, uses OKX's application-level text ping/pong heartbeat and reconnects with exponential backoff if the pong does not arrive. All timestamps are UTC. The collector follows OKX's [official WebSocket candlestick channel](https://www.okx.com/docs-v5/en/#websocket-api-public-channel) and resolves live instruments from the [public instruments metadata](https://www.okx.com/docs-v5/en/#rest-api-public-data). Only public market endpoints are used; no OKX API keys or trading permissions are used or supported.

### Run locally

```sh
python -m pip install -r requirements.txt
python candle_service.py
```

The default port is `8080`; set `PORT` to override it. Set `CANDLE_DATA_FILE` if you want a local JSON copy. Run the unit tests with `python -m unittest discover -s tests`.

### Always-on deployment option

`render.yaml` defines an opt-in Render web service using the Dockerfile and an always-on paid `starter` plan. To deploy, connect this GitHub repository to Render, select **Blueprint** deployment, review the plan, and create the service. `autoDeployTrigger` is set to `off`, so later repository changes require a manual deploy. Render provides the HTTPS service URL; append `/candles.json` for the data and `/healthz` for status.

No secret is needed for OKX. The feed endpoint contains public market data and is read-only. Keep the existing GitHub Actions workflow enabled for the BTC snapshot; the continuous feed runs independently and does not overwrite `data/latest.json`. Deployment requires the owner to connect the repository to a Render account and approve the paid always-on service.
