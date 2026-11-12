# BTC Market Data

Free public-data collector for a Bitcoin market dashboard. No exchange credentials or paid API keys are required.

## What it collects
- Spot BTC prices (Binance public market-data domain, OKX, Bybit alternate host)
- Per-exchange derivatives: perpetual price, funding and open interest
- Aggregate derivatives context and liquidation/history feeds (CoinBoss, SwoleCharts, Edge or Not)
- US spot Bitcoin ETF daily net flows (TFTC open dataset)
- US macro series from FRED CSV: 10Y Treasury yield (DGS10), broad trade-weighted USD index (DTWEXBGS), Fed funds rate (FEDFUNDS), CPI (CPIAUCSL), core CPI (CPILFESL), unemployment (UNRATE)

## Files
- `data/latest.json`: latest raw source payloads, retrieval timestamps, latency, endpoint and source status
- `data/quality.json`: freshness and payload validation, required-source failures and overall status

A source error is recorded, not silently replaced with an old value. Optional sources may fail without invalidating the entire snapshot. Required-feed failures mark the snapshot `degraded`, but the workflow still saves the diagnostic snapshot.

## Schedule
GitHub Actions runs on a 15-minute cron and after collector/validator/workflow code changes. GitHub scheduled workflows can start late; this is not a real-time feed or a guaranteed 15-minute SLA.

## Run manually
Repository → Actions → Update BTC Market Data → Run workflow.

## Data caveats
ETF flows are daily/session data and may be incomplete or revised by the provider. FRED observations update on their own release calendars, not every collection cycle. Retrieval freshness is not the same as observation freshness. Always inspect each source's timestamp and underlying observation date before interpreting it.

All feeds are public endpoints. Do not add API keys, passwords, exchange credentials or personal data to this public repository.
