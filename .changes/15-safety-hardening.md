---
issue: 15
agent: root
type: Security
date: 2026-08-06
---

Harden dry-run orchestration so unknown attack UI and exhausted capacity halt before any attack intent or dispatch record is created.

- Configuration: no change; `dry_run=true` remains mandatory by default
- Database: no writes occur for these rejected states beyond append-only state events
- Verification: end-to-end tests cover unknown UI and game-reported full capacity
- Safety: prevents dry-run from masking unsafe or impossible dispatch conditions
- Rollback: revert this change; prior behavior is preserved only in Git history
