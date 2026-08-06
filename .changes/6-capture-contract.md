---
issue: 6
agent: root
type: Fixed
date: 2026-08-06
---

Capture manifests now conform to the dataset integrity contract and require explicit
baseline eligibility. Legacy mail-list captures are rejected by the runtime parser.

- Safety: the obsolete `mail-list-v1` UI cannot be used for navigation.
- Verification: capture output round-trips through manifest validation.
