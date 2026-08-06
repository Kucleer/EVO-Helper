---
issue: 14
agent: root
type: Changed
date: 2026-08-06
---

Plan ranges now require the expected fleet-preset signature, and persisted scan
plans receive a stable public UUID plus an auditable update timestamp.

- Safety: a persisted range can no longer omit the preset signature used by the
  final dispatch check.
- Database: migration `8c41b9d201ff` backfills public IDs and timestamps.
