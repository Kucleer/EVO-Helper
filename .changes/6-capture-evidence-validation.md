---
issue: 6
agent: root
type: Added
date: 2026-08-06
---

Validate complete live-capture evidence metadata in addition to image hashes.

- Impact: each browser sample must carry auditable session, artifact, screen, version, viewport, source, and batch metadata.
- Configuration: none.
- Database: no schema change.
- Verification: 124 pytest tests, Ruff, formatting, mypy, and the current five-sample live manifest.
- Safety: malformed or misclassified captures cannot become trusted current UI evidence.
- Rollback: revert the validator and documentation together.
