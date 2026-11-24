# Gold Radar — feasibility and acceptance criteria
Status: research only; no live signals, sizing, orders or deployment.
Venue: OKX Europe. Target: XAUUSD UM X-Perp, expiry 2031-05-02. Candidate API identifier XAU-USD_UM_XPERP-310502 is UNVERIFIED. Never substitute another contract.
Public specification (https://okx.actor/en-eu/help/x-perps-contract-specifications, commodity section updated 2026-08-10): 0.001 XAU, tick 0.1 USD, funding 8h, 24/7, up to 10x. Require exact EEA API instrument match, ctVal, ctValCcy, ctMult, lotSz, minSz, expiry, settlement currency; mismatch or unavailable => block sizing and signals.
## Collection
Use existing EEA transport after inspecting repository implementation. Collect closed 1m/5m/15m/1H/4H/1D candles, ticker, mark/index, book, trades, OI and funding. Measure spread, depth, expected slippage, basis, timestamp and gaps; book/trades older than 180s unusable. Separate underlying-market open/closed, weekend and holidays. Macro: timestamped USD index, US nominal/real yields, scheduled Fed/CPI/NFP/PCE; distinguish release time from observation time; do not treat daily series as live. No paid data commitments. Missing macro feed explicitly unavailable.
## Model
Independent gold model, BTC not mandatory filter. Primary 1–24h (evaluate 6/12/24h), secondary 5–40m. Separate anticipated SHADOW and confirmed BASELINE; initially both simulation only. ATR-based stops, retests, regime features; no fabricated probabilities. Event risk warnings, no assumption that scheduled releases predict direction.
## Validation
Walk-forward with chronological splits, closed candles, no look-ahead, costs and funding, conservative ambiguous TP/SL ordering, compare matched risk and time windows to ZEC. Measure sample size, fill, TP-before-SL, expectancy, drawdown, lead time, spread/slippage. Observe 14–30 days initially and extend if insufficient. No promotion without reviewed evidence.
## Deployment
Keep existing BTC/ZEC code, workflows, alerts and production untouched. Check free infrastructure capacity and API limits before adding collector. Gold dashboard and Telegram only after feed validation; no automatic execution or sizing. No duplicated scheduled tasks. 
