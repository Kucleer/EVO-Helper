---
issue: 6
agent: root
type: Changed
date: 2026-08-06
---

Document browser capture, reconnect, and independent UI-version gates, and enforce
the current-mail baseline classification in the capture tool and manifest validator.

- Impact: the current UI collection process has a fail-closed reconnect path and explicit evidence requirements; non-mail captures cannot enter the current-mail baseline.
- Configuration: none.
- Database: no schema change.
- Verification: capture CLI and manifest validation tests, plus the full Python quality suite.
- Safety: reconnect cannot cause a repeated entry click or bypass `ActionGuard`.
- Rollback: revert the documentation and baseline classification guard together.
