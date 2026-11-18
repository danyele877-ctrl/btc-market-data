# Radar architecture decisions

| ID | Decision | Reason | Status |
|---|---|---|---|
| ADR-001 | GitHub is the versioned source of truth for code, tests, policy, and documentation | Reproducibility and reviewable changes | Active |
| ADR-002 | JSON archives under `data/` remain the current operational history store | Already deployed through the bridge; avoid an unconfigured database dependency | Active |
| ADR-003 | A managed time-series database is a future migration, not assumed to exist | Requires hosting, secrets, backup, restore, and parity tests | Proposed |
| ADR-004 | `radar_snapshot.json` is the canonical compact payload for the alert | Separates analysis/history from presentation | Active |
| ADR-005 | Alert readiness is gated by a machine-checkable contract | Prevents code-only changes from being mistaken for complete alert integration | Active |
| ADR-006 | BASELINE remains protected and SHADOW remains isolated and experimental | Prevents unvalidated logic from changing the established reference | Non-negotiable |
| ADR-007 | CTVAL and order/sizing safety gates remain closed until OKX resolves the discrepancy | Prevents invalid contract-value conversions | Non-negotiable |

## Update procedure

Append a new row rather than rewriting an earlier decision. If a decision is superseded, mark the old row as superseded and link the replacement decision. Record the pull request and commit in the changelog.

| ADR-008 | BASELINE publishes separate LONG/SHORT evidence contributions and vetoes unconfirmed/extended entries symmetrically | Exposes conflicting directional evidence and reduces chasing into nearby structure; scores remain heuristic, not probabilities | Active on feature branch; pending review/merge |
