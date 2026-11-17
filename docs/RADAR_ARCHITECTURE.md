# BTC + ZEC Futures Radar — architecture and data lifecycle

## Source of truth

The GitHub repository is the versioned source of truth for code, tests, configuration, operating rules, architecture decisions, and change history. Changes are made on a branch, tested in GitHub Actions, reviewed in a pull request, and merged only with explicit authorization.

The repository is **not** itself a relational database. At present, operational history is stored in structured JSON archives under `data/` and carried forward by the existing GitHub Actions bridge. These files are useful for reproducibility and diagnostics, but Git history is not an ideal long-term high-volume time-series database.

## Four layers

1. **Code and policy — GitHub**
   - Predictive engines, BASELINE and SHADOW logic, contract safety, tests, workflow definitions, and documentation.
   - Each behavior change must be versioned and tested.
2. **Operational history — structured archives**
   - `data/zec_prediction_tracking.json`: BASELINE/SHADOW prediction records and matured diagnostic outcomes.
   - `data/zec_predictive_archive.json`: predictive samples and experimental estimates.
   - `data/zec_context_archive.json`: context observations, setups, and movement-event outcomes.
   - `data/radar_snapshot.json`: latest compact output consumed by the alert/bridge.
   - Retention, deduplication, UTC timestamps, source provenance, and atomic writes must be preserved.
3. **Processing — Radar engine**
   - Collects and validates feeds, evaluates market regime and directional candidates, records prediction outcomes, and builds the compact snapshot.
   - Missing, stale, or contradictory data must remain visible and must not be silently replaced with stale values.
4. **Presentation — alert**
   - The alert is a compact operational view of the latest published snapshot, not the only place where logic or history lives.
   - It must display data freshness/health, BTC regime context, ZEC BASELINE, ZEC SHADOW (clearly labelled experimental), decision and veto/reasons, CTVAL/funding blockers, and prediction-history diagnostics when available.
   - It must never convert heuristic scores into probabilities or diagnostic outcomes into real fills/PnL.

## Alert integration contract

`radar_alert_contract.py` validates that the snapshot contains all required sections and that safety invariants remain intact. The test suite and CI run this check against the published snapshot. A component is not considered integrated merely because its code exists: its output must be represented in the snapshot, the bridge must publish it, and the alert contract must pass.

## Storage evolution

### Current state
JSON archives are committed/published by the existing bridge. This is the deployed storage mechanism today. Do not describe it as a database or imply that a separate database has already been provisioned.

### Recommended next phase
When archive volume or query needs justify it, add a managed append-only time-series store (for example PostgreSQL/Timescale-compatible storage) behind a small repository interface. Keep JSON snapshots as portable exports and alert payloads. The database migration must include:
- unique prediction/event IDs and idempotent upserts;
- UTC event, capture, and maturity timestamps;
- source/instrument/timeframe provenance;
- immutable BASELINE records and separate SHADOW namespace;
- retention and backup/restore tests;
- export/replay parity against the current JSON archives;
- no credentials or connection strings committed to Git.

Do not introduce a database dependency until hosting, credentials, backup policy, and migration tests are configured. Until then, the current JSON archive remains the explicit operational store.

## Non-negotiable safety boundaries

- BASELINE is protected; SHADOW cannot write to or replace BASELINE outputs.
- SHADOW remains labelled `IN RACCOLTA` until separately validated.
- CTVAL remains unresolved while the public 0.1 ZEC/contract specification and account-specific API 0.01 ZEC value are unreconciled.
- No contract conversion, CVD/OI conversion, quantity, exposure, margin, or order sizing while CTVAL is unresolved.
- Orders remain disabled. The configured maximum risk is policy metadata, not permission to size or trade.
- Funding timestamps that conflict with the confirmed 4-hour specification remain raw diagnostics, not a basis for projected funding.
- Every score or empirical frequency must retain its experimental/uncalibrated label until statistically validated.

## Change completion checklist

- [ ] Change implemented on a feature branch.
- [ ] Unit tests cover normal, missing-data, and safety-boundary cases.
- [ ] Snapshot builder emits the new field(s).
- [ ] Bridge publishes the new snapshot without stale overwrite.
- [ ] Alert contract and CI pass.
- [ ] Changelog and decision record updated.
- [ ] BASELINE regression checks pass.
- [ ] Pull request reviewed; merge only after explicit approval.
- [ ] Post-merge workflow and live snapshot verified.
