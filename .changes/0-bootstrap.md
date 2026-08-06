---
issue: 0
agent: root
type: Added
date: 2026-08-06
---

Bootstrap EVO-Helper with frozen contracts and a safe-by-default configuration.

- Configuration: `dry_run=true`, loopback-only HTTP binding
- Database: schema implementation deferred; contract and timestamp rules frozen
- Verification: domain contract tests cover coordinate iteration and state transitions
- Safety: no implementation path can issue a real dispatch
- Rollback: remove the bootstrap commit; legacy archive remains external to the repository
