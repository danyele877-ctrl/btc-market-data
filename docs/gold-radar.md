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

## Registro e replay

Il registro salva livelli e scadenze all'apertura dell'ipotesi. Il riconciliatore legge
soltanto candele chiuse del contratto XAU su OKX Europe e richiede continuità dal cursore.
Un attraversamento con volume positivo è un'evidenza di prezzo per il modello. Il
registro distingue stop, livelli raggiunti, scadenza, invalidazione e ordine intrabar
indeterminato. Gli esiti legacy recuperati conservano il risultato precedente in
`legacy_outcome`. Il processo usa le finestre REST in background su `/gold-state`.

TECHNICAL V1 e SWING V1 restano riferimenti. TECHNICAL V2 Scalping, TECHNICAL V2
Intraday, Reversal, Breakout, Pullback e Swing V2 sono strategie sperimentali.
`CONDITIONAL_WATCH` e `PAPER_TEST_CANDIDATE` sono coorti separate. Il backtest
usa l'ora di chiusura delle candele, periodi cronologici separati, una simulazione
alla volta per strategia e validità, e commissioni teoriche maker 0,02% e taker
0,05%. Rendimento e drawdown si riferiscono a esposizione teorica senza leva.
Passare `--funding` con una mappa JSON fundingTime → realizedRate consente di
misurare anche il funding osservato sulle posizioni applicabili. Il mark al
settlement è approssimato con la candela XAU confermata. Spread storico, coda e
slippage richiedono dati di microstruttura che il replay non presuppone.

```sh
python gold_backtest.py --data /tmp/xau-history --fetch --output /tmp/xau-replay.json
python -m unittest discover -p '*test.py'
docker build -f Dockerfile.gold -t gold-test .
```

L'interfaccia usa `GET /dashboard.json`, `GET /live.json`, `GET /paper.json`
e `GET /healthz`. Il canale Telegram GOLD si attiva soltanto con le variabili
`GOLD_TELEGRAM_BOT_TOKEN` e `GOLD_TELEGRAM_CHAT_ID` configurate sul servizio.
`GOLD_TELEGRAM_INCLUDE_CONDITIONAL=1` include le coorti condizionate.
