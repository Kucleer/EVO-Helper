---
issue: 10
agent: vision-game
type: Added
date: 2026-08-06
---

Add battle detail and battle replay parsing with fleet composition extraction.

- Configuration: `battle_detail_ui_version` and `battle_replay_ui_version`
  tracked independently
- Database: none
- Verification: unit tests cover both-sides fleet parsing, coordinate
  attribution, and raw-time-to-UTC normalization (game local time interpreted
  as UTC+8)
- Safety: low-confidence or missing coordinates are surfaced, never guessed
- Rollback: remove the battle parsing module
