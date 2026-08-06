---
issue: 14
agent: root
type: Fixed
date: 2026-08-06
---

Close the persistent run-state loop for capacity recovery and report draining.

- Impact: a capacity retry is auditable, successful report draining reaches `COMPLETED`, and a failed report-page navigation pauses the run.
- Configuration: none.
- Database: no schema change; state events remain append-only.
- Verification: end-to-end SQLite runner tests plus the full pytest, Ruff, and mypy suite.
- Safety: report navigation failure cannot be mistaken for completed work.
- Rollback: revert this change to retain the prior runner behavior.
