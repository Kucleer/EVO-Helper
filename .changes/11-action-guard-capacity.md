---
issue: 11
agent: vision-game
type: Added
date: 2026-08-06
---

Add the ActionGuard safety gate, line-capacity checks, and a simulated game
adapter; no code path can click without a guard-cleared, single-use token.

- Configuration: `dry_run=true` always refuses dispatch; `pyautogui.FAILSAFE`
  remains enabled
- Database: none
- Verification: unit/integration tests cover dry-run refusal, token
  single-use/expiry, pre-click re-observation, known-target enforcement, and
  capacity conflicts
- Safety: dispatch refusal on dry-run, unknown UI version, low confidence,
  stale re-observation, or conflicting capacity sources
- Rollback: remove the game package and restore direct adapter usage
