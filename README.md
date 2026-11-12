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
