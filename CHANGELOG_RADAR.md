# BTC + ZEC Futures Radar — change log

## 2026-11-17 — Operational architecture baseline

- Defined GitHub as the versioned source of truth for code, tests, policies, and documentation.
- Recorded that current prediction and context history is stored in JSON archives under `data/`, not in a separately provisioned database.
- Defined `data/radar_snapshot.json` as the compact payload consumed by the alert/bridge.
- Added an alert integration contract and tests for required sections and safety invariants.
- Added a documented future database migration path without introducing an unconfigured external dependency.
- Preserved BASELINE/SHADOW separation, unresolved CTVAL lock, diagnostic-only tracking, and disabled orders/sizing.
- Pending: run CI and inspect the live published snapshot after this branch is merged. No merge has been performed by this change.
