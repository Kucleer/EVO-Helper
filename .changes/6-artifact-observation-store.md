---
issue: 6
agent: root
type: Added
date: 2026-08-06
---

Add immutable evidence storage and UI-observation indexing for live captures.

- Impact: captured files receive SHA-256-backed database artifacts and can be linked to versioned UI observations.
- Configuration: artifact root and source are provided by the application adapter.
- Database: uses the existing `artifacts` and `ui_observations` tables; no migration needed.
- Verification: integration tests cover file persistence, database indexing, observation linkage, and rejection of unknown artifacts.
- Safety: observations cannot claim evidence that was never indexed; evidence paths cannot escape the configured root.
- Rollback: remove the artifact adapter; existing evidence files and rows remain auditable.
