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
- The Render candle service uses WebSocket for live 1m/5m candles; code changes are deployed automatically only after GitHub Actions checks pass; the Render build filter ignores collector-only, data, documentation, and test changes.
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

`render.yaml` defines an opt-in Render web service using the Dockerfile and an always-on paid `starter` plan. To deploy, connect this GitHub repository to Render, select **Blueprint** deployment, review the plan, and create the service. `autoDeployTrigger` is set to `checksPass`, with a build filter limited to `candle_service.py`, `Dockerfile`, and `requirements.txt`; Render waits for successful GitHub Actions checks before deploying runtime changes. Render provides the HTTPS service URL; append `/candles.json` for the data and `/healthz` for status.

No secret is needed for OKX. The feed endpoint contains public market data and is read-only. Keep the existing GitHub Actions workflow enabled for the BTC snapshot; the continuous feed runs independently and does not overwrite `data/latest.json`. Deployment requires the owner to connect the repository to a Render account and approve the paid always-on service.

## ZEC Early Signal Engine (simulation only)

`build_radar_snapshot.py` adds a `zec_early_signal` object to the existing `data/radar_snapshot.json` bridge document. The engine evaluates only completed, fresh 1m/5m candles and pins futures analysis to `ZEC-USD_UM_XPERP-310530`. It combines independent groups for continuous WebSocket CVD plus sequenced order-book imbalance and its short-term change, paired open-interest/price context when available, 5m ZEC-vs-BTC relative strength, and 1m/5m candle microstructure. Possible absorption is labelled cautiously from price/CVD divergence. Related CVD and book evidence is kept inside one order-flow group to limit double counting.

The output reports `PRE-ALLERTA`, `SETUP IN FORMAZIONE`, `TRIGGER CONFERMATO`, or `ATTENDERE`, with heuristic directional points (not win probabilities), an uncalibrated confidence score, favorable and contrary evidence, blockers/limitations, and a structural invalidation condition. Stale, incomplete, non-contiguous or wrong-instrument inputs force `ATTENDERE`. Missing OI or liquidation data is disclosed and lowers confidence; these optional feeds do not fabricate a signal. Liquidations are currently unavailable in the feed.

The engine is simulation-only and orders remain disabled. For the exact EEA/OEM instrument `ZEC-USD_UM_XPERP-310530`, the OKX specification updated 2026-11-17 and the public instrument API both report `ctVal=0.01 ZEC`, `ctMult=1`, tick `0.01`, and the API reports lot/min size `1`. The earlier 0.1 value is superseded for this EEA specification; `CTVAL_RECONCILED=false` remains a safety gate and the visible CTVAL is diagnostic only. Formula checks use base ZEC = contracts × ctVal × ctMult; USD notional = base ZEC × price; gross USD-margined PnL = base ZEC × (exit − entry) for LONG and the inverse for SHORT. Fee = notional × explicitly supplied fee tier; funding cash flow is the signed position value × rate; initial margin = position value/leverage and maintenance margin = position value × tier MMR. No account-specific fee tier, leverage, MMR, margin, quantity or exposure is inferred or published. TP/SL remain price/ATR barriers. Quantity sizing and order placement remain disabled. Funding output continues to compare API timestamps to the 4-hour ZEC specification and retains a warning when timestamps conflict. No CoinMarketCap data is used.

Run regression tests with `python -m unittest discover -s tests -v`. Replay committed bridge snapshots, without PnL or order simulation, using `python replay_zec_signals.py` (optionally `--limit N`). Replay candle outcomes are descriptive only: bridge observations are sparse, overlapping and drawn from a short single-market regime, so they do not establish statistical performance.


## ZEC Breakout Early Warning (simulation only)

The existing Early Signal Engine now emits a separate `breakout_early_warning` diagnostic. It distinguishes pre-breakout preparation near recent 5m support/resistance, a volume/relative-strength-confirmed breakout, an unconfirmed break, and a move already extended enough to wait for a retest. It reports the measured resistance/support, 5m volume ratio, recent range-compression ratio, ZEC/BTC relative strength, available CVD and paired OI change. Missing inputs remain explicitly unavailable; volume confirmation is never inferred from price alone.

This diagnostic does not change the Early Signal Engine score, stage, entry decision, schedule, CTVAL reconciliation, risk ceiling, sizing flags or order permissions. It is a separate simulation-only warning and never places or sizes orders. A breakout warning is not an entry instruction; the existing freshness, exact-instrument and contract-safety gates remain authoritative.

## ZEC Predictive Engine (simulation only)

The existing build_radar_snapshot.py now also runs zec_predictive_engine.py and writes zec_predictive_engine into the same data/radar_snapshot.json. It consumes the exact OKX EEA X-Perp ZEC-USD_UM_XPERP-310530, completed 1m/5m/1H/4H history, and only updates the archive when the snapshot, health endpoints, WebSocket and required 200-bar history pass freshness/continuity checks.

The persistent data/zec_predictive_archive.json stores timestamped feature vectors (returns, ATR, range, volume, RSI, trend and BTC-relative strength for scalping) and forward outcomes. Primary scalping samples use completed 5m bars and 1m forward paths for 5/15/30/40-minute horizons. Secondary reversal samples use completed 1H bars and 5m forward paths for 1/2/4-hour horizons. The first version uses explainable nearest-neighbour analogs and publishes a touch/depletion frequency only after at least 30 mature analogs; the conditional TP-before-SL frequency has its own threshold of at least 30 filled analogs with fully matured outcomes for that direction/horizon. Estimates are empirical and explicitly uncalibrated; the archive is not proof of predictive edge.

A hypothetical limit fill is labelled only when the historical OHLC range touches an entry offset of 0.20 ATR within the defined fill window. This is a TOUCH PROXY, not a real fill probability: public candle data cannot reconstruct queue priority, order-book queue depletion, latency, spread or partial fills. TP/SL outcomes use 1.5 ATR / 1.0 ATR barriers; ambiguous same-candle touches are conservatively labelled as SL. Filled observations may be stored with a pending TP/SL outcome; subsequent runs update that outcome only after the full forward horizon has matured.

No order placement or private API credentials are enabled. Automated position sizing, leverage, margin, exposure and PnL output remain disabled in the predictive engine. The exact EEA instrument specification and public API agree on `ctVal=0.01 ZEC`, `ctMult=1`; this is a verified formula basis only, while `CTVAL_RECONCILED=false` and all execution/sizing gates remain false. The risk ceiling is not a calculated position risk. The existing GitHub Actions schedule and Render deployment filters are unchanged.


## ZEC Context Archive and trigger validation

The radar now writes a separate `data/zec_context_archive.json` alongside the OHLC predictive archive. It records at most one quality-gated observation per UTC minute from the exact OKX EEA X-Perp: WebSocket CVD 1m/5m/15m in contracts, book imbalance top5/10/20, short book-imbalance change, spread in basis points, OI in contracts, feed ages and sequence-gap counters. Observations are kept for up to 30 days. CVD, book imbalance and OI remain in their original contract units. CTVAL-derived values are diagnostic only; no context archive value enables sizing or orders.

Only fresh schema-v3 snapshots with the exact instrument and healthy, connected, sequence-valid microstructure are archived. Missing/stale data are skipped rather than filled or interpolated. The bridge stores the context archive in the same GitHub publication step as the radar snapshot.

Confirmed Early Signal triggers are recorded as separate episodes with a 40-minute same-direction cooldown (5-minute opposite-direction cooldown). Forward outcomes are measured from the completed trigger candle close, using 1m bars and ATR barriers (SL 1 ATR, TP1 1.5 ATR) at 5/15/30/40/60/120/240 minutes. Ambiguous same-candle TP/SL is conservatively labelled SL_AMBIGUO; no-barrier outcomes remain NONE. These are signal-close reference outcomes, not actual limit fills, not net PnL, and not independent trade win-rates.

The predictive engine also builds separate microstructure-enhanced analog families, using only observations timestamped at or before each completed candle close (no look-ahead): log-signed CVD windows, book imbalance, book-imbalance change, spread and OI change, plus price/volatility/volume/RSI/trend and BTC-relative strength. These estimates remain experimental and separate from OHLC-only analogs. The secondary 1H model uses paired BTC 1H bars when available.


The bridge replay step also backfills the context archive from up to 600 previously published radar snapshots. Historical observations are revalidated using each snapshot's own capture time; trigger outcomes are only matured when the complete minute-by-minute forward path is present. This avoids treating a truncated candle tail as a full 15–240 minute outcome.


## ZEC movement event study

The context archive also detects completed 5m/15m price displacements that exceed a volatility-scaled threshold. Each event is linked only to the latest valid microstructure observation captured before the event candle closed: CVD 1m/5m/15m, top-10 book imbalance and its short change, spread, OI change, funding rate, and paired BTC 1m relative return when available. The event record also stores subsequent 5/15/30/40/60/120/240-minute ATR-barrier outcomes when the complete forward path is available.

The event study describes co-occurring factors and subsequent price paths. It does not infer that CVD, book imbalance, OI, funding or BTC caused the move. Missing or stale inputs remain missing; no interpolation or post-event feature leakage is allowed.


## Queue-aware LIMIT execution research

The Render service now exposes a bounded 15-minute WebSocket trade tape (trade ID, aggressor side, price, size in contracts, count and gap flag) plus sequenced top-10 book-depth snapshots sampled at approximately one-second intervals. The bridge merges trades by trade ID, preferring the WS record where REST has the same trade, and deduplicates WebSocket book states by `seqId` (REST snapshots by exchange timestamp). REST polling gaps are not treated as live WS gaps. It records per-capture truncation and sequence-health metadata and retains up to 15,000 WebSocket depth snapshots (about four hours) and 30,000 unique trades.

The separate zec_queue_model.py evaluates a hypothetical passive LIMIT by reading the displayed queue at the candidate price and summing aggressive trades at or through that price during the five-minute validity window. It only labels a historical sample when the entry book, book sequence coverage, capture checks and trade tape pass quality gates. If the limit is marketable, book depth is missing, the tape is truncated, or a sequence gap occurs, it returns no estimate. The engine trains separate scalp queue families at 0.10, 0.20, 0.30 and 0.40 ATR offsets, and exposes queue-depletion frequency separately from OHLC touch frequency. Where each offset has sufficient mature analogs, it compares the experimental joint queue-depletion/TP1-before-SL frequency; this is only a candidate offset ranking, not a calibrated optimum.

Queue depletion is still only a proxy for fill: cancellations, hidden liquidity, exact queue position, network latency and partial fills cannot be reconstructed from public aggregated market data. It must never be described as a guaranteed fill or a calibrated execution probability. No orders are sent and no position size is computed.


The Render candle service now warms up and retains up to 500 completed 1m/5m candles (the bridge preserves the same 500-bar limit). This provides about 8h20 of 1m paths for scalp labels and about 41h40 of 5m paths for 1H/2H/4H reversal labels, rather than limiting reversal training to roughly one day.


## Radar architecture, history, and alert integration

The durable architecture and operating rules are documented in [docs/RADAR_ARCHITECTURE.md](docs/RADAR_ARCHITECTURE.md), with decisions in [docs/RADAR_DECISIONS.md](docs/RADAR_DECISIONS.md) and changes in [CHANGELOG_RADAR.md](CHANGELOG_RADAR.md).

The current alert payload is `data/radar_snapshot.json`. Historical prediction/context records remain in the JSON archives under `data/`; these are the current operational store, **not a separately provisioned database**. A managed time-series database is a future migration requiring configured hosting, secrets, backups, and replay/parity tests.

CI runs `radar_alert_contract.py` against the published snapshot. This checks that BASELINE, SHADOW, CTVAL safety, prediction tracking, and predictive-engine sections are present and that orders/sizing remain disabled. A passing contract is required for alert integration; scores remain diagnostic unless independently calibrated.
