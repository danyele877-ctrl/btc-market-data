# BTC + ZEC Futures Radar — change log

## 2026-11-18 — Live feed integrity and nowcast gates

- Tightened the completed 5m nowcast freshness limit to 180 seconds; stale data leaves the radar visible but forces `ATTENDERE`.
- Kept SHADOW's existing 360-second 5m diagnostic gate separate; BASELINE's stricter stale blocker is not treated as a SHADOW strategy change.
- Separated REST polling history diagnostics from WebSocket trade-gap/CVD diagnostics; REST `tradeId` sparsity no longer represents live WS continuity.
- Rejected materially future-timestamped WS trades and excluded any event after the evaluation time from rolling CVD windows.
- Preserved sampled WebSocket book `prevSeqId` and deduplicated WS states by `seqId`, retaining valid states that share a millisecond with another state or REST capture.
- Added cross-market candle timestamp checks for BTC spot, ZEC spot, and ZEC X-Perp on 1m and 5m.
- Added regression tests and architecture notes. Direction scoring/thresholds, SHADOW's existing freshness criterion, order permissions, sizing gates, and workflow schedules did not change; the BASELINE 5m freshness safety gate is now 180 seconds.

## 2026-11-17 — Operational architecture baseline

- Defined GitHub as the versioned source of truth for code, tests, policies, and documentation.
- Recorded that current prediction and context history is stored in JSON archives under `data/`, not in a separately provisioned database.
- Defined `data/radar_snapshot.json` as the compact payload consumed by the alert/bridge.
- Added an alert integration contract and tests for required sections and safety invariants.
- Added a documented future database migration path without introducing an unconfigured external dependency.
- Preserved BASELINE/SHADOW separation, unresolved CTVAL lock, diagnostic-only tracking, and disabled orders/sizing.
- Pending: run CI and inspect the live published snapshot after this branch is merged. No merge has been performed by this change.
