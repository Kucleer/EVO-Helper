---
issue: 14
agent: root
type: Fixed
date: 2026-08-06
---

Integration workflow outcomes now synchronize the persisted run aggregate:
capacity waits become `WAITING_CAPACITY`, exhausted ranges become `DRAINING`,
and safety failures become `PAUSED`.
