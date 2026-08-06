---
issue: 8
agent: vision-game
type: Added
date: 2026-08-06
---

Implement the framework-free vision pipeline with pluggable YOLO/OCR/template
engines, page/UI-version classification, and strict multi-source fusion.

- Configuration: vision confidence gates are code constants
  (`CoordinateFusion` requires 3 agreeing sources at >=0.995; name fusion at
  >=0.99); real engines are optional adapters
- Database: none
- Verification: unit tests cover page classification, unknown-UI refusal,
  three-source coordinate consistency, and frame stability
- Safety: unknown pages and conflicting sources produce no usable observation;
  calls must stop safely
- Rollback: remove the vision package and its tests
