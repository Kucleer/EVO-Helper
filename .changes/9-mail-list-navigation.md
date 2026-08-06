---
issue: 9
agent: vision-game
type: Added
date: 2026-08-06
---

Add new mail-list parsing and safe navigation support for mail-list-v2.

- Configuration: `mail_list_ui_version` tracked independently
- Database: none
- Verification: parser extracts owner/coordinate pairs from mail items; unknown
  versions raise `UnknownUiVersionError` so callers stop and preserve a
  diagnostic capture
- Safety: never falls back to the legacy mail-list click path
- Rollback: remove the mail-list parser module
