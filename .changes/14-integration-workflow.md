---
issue: 14
agent: root
type: Added
date: 2026-08-06
---

Add the safe application workflow that closes scanning, dry-run dispatch recording, and report draining.

- Configuration: workflow defaults to `dry_run=true`
- Database: uses existing append-only repository records and persisted coordinate claims
- Verification: end-to-end simulator test verifies scan, dry-run, draining, report persistence, and restart cursor use
- Safety: dry-run records an intent/dispatch but never invokes the game dispatch method
- Rollback: revert this change; individual adapter and repository components remain independent
