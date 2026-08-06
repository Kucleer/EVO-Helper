---
issue: 7
agent: vision-game
type: Added
date: 2026-08-06
---

Add dataset manifest tooling and annotation rules for legacy UI samples.

- Dataset: `datasets/manifests/legacy-source-20260806.json` verified against the
  six archived legacy images; legacy mail-list samples are marked ineligible for
  the current mail baseline while battle detail/replay samples remain eligible
  for regression
- Configuration: none
- Database: none
- Verification: manifest hash, sample-count, and legacy-baseline pollution
  checks covered by unit tests
- Safety: enforces the rule that legacy mail-list fixtures never enter the
  current mail-list training or regression set
- Rollback: remove the manifest annotation tools and revert the manifest
