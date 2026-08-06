---
issue: 14
agent: root
type: Security
date: 2026-08-06
---

Prevent dry-run dispatch records from closing battle reports and prove cursor recovery with a restart-level SQLite workflow test.

- Configuration: no change
- Database: report matching now considers only accepted non-dry-run dispatches
- Verification: persistent SQLite end-to-end restart test and dry-run report mismatch test
- Safety: a simulated action cannot be mistaken for a real game action
- Rollback: revert this change; no migration is required
