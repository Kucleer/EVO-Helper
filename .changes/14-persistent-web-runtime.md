---
issue: 14
agent: root
type: Added
date: 2026-08-06
---

Added the SQLite-backed Web application service and persistent application
factory. Plan CRUD, run lifecycle, targets, history, revisits, and diagnostics
now use the existing storage schema rather than in-memory demo state.

- Verification: configuration and API-created plans survive app recreation.
- Safety: runtime defaults remain dry-run and all existing local-token checks
  continue to be enforced by the shared FastAPI factory.
