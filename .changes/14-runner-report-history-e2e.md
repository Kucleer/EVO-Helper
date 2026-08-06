---
issue: 14
agent: root
type: Added
date: 2026-08-06
---

Add an end-to-end SQLite proof that report draining stores fleet history before the run completes.

- Impact: the CP4 workflow evidence now covers real report and fleet-snapshot persistence, not only an empty drain.
- Configuration: none.
- Database: no schema change.
- Verification: 122 pytest tests, Ruff, formatting, and mypy.
- Safety: a run reaches `COMPLETED` only after the report-drain operation succeeds.
- Rollback: revert this test-only evidence addition.
