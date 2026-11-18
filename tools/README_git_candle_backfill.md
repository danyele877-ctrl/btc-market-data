# Historical Git candle backfill (research-only)

This tool reconstructs a derived BTC/ZEC OHLCV archive from historical Git versions of the source feeds.

## Safety boundary
- Run only on `research/*`; the tool refuses to run on `main`.
- Reads Git history and blobs only; never rewrites/deletes tracked source archives.
- Writes only `data/derived/git_candle_history.json` and its manifest.
- Does not import the live radar, exchange clients, account credentials, sizing, or order code.
- The output is research data, not a live feed and not evidence of real fills.

## Run
Use a full clone (not a shallow clone), then:

```bash
git switch research/historical-backfill-2026-11
git fetch --all --tags
python tools/backfill_git_candles.py
```

Optional lower bound / output path:

```bash
python tools/backfill_git_candles.py --since 2026-08-01 --out data/derived/git_candle_history.json
```

The default source paths are the historical radar snapshot, Render feed, and standalone ZEC X-Perp history. The tool deduplicates identical source blobs, validates candle boundaries and OHLC consistency, then merges on instrument + timeframe + opening timestamp. Each candle retains commit/blob/path provenance. Conflicting values for the same key are preserved in a separate `conflicts` array and counted in the manifest; the canonical row is chosen deterministically using the newest available snapshot/commit timestamp.

## Review gate
Do not connect this archive to the radar until the manifest has been reviewed, all conflicts explained, series coverage and gaps checked, and independent tests have passed. Missing BTC/ZEC spot 1m/5m history must remain explicitly missing; do not synthesize bars from snapshot tails or futures candles.
