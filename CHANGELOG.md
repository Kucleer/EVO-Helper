# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Initial safety-first project bootstrap, frozen domain contracts, and dry-run defaults.
- Domain & persistence (Wave 1): lexicographic coordinate ranges and cursor-based claiming,
  UTC+8 time-window scheduling with cross-day arming and DRAINING semantics, weekly-cycle
  dedupe and idempotent starts, SQLAlchemy 2 schema (all plan 8.1 tables), Alembic initial
  migration, append-only history with strict report-to-dispatch matching, and fleet diff
  computation (issues #4, #5).
- Vision pipeline (Wave 1): pluggable YOLO/OCR/template engines with safe offline fallbacks,
  deterministic UI parsers (mail list, battle detail, battle replay, galaxy, preset
  signature), multi-frame consistency, and three-source coordinate fusion with a 0.995
  confidence gate; 7/21 legacy UI annotation rules (issues #7, #8, #10).
- Game safety adapter (Wave 1): ActionGuard single-use short-lived dispatch tokens, fresh
  re-observation immediately before any click, line-capacity gate combining user limit,
  game feedback, and in-flight fleets (issue #11).
- Web/API (Wave 1): loopback-only FastAPI application with plans CRUD, idempotent manual run
  start, pause/resume/emergency-stop, run status, bot targets, coordinate history and fleet
  diff pages, forced revisits, and diagnostics; same-origin/local-token protection for
  mutating endpoints; application-service seam with an in-memory fake for tests (issues #12,
  #13).
- Mail list navigation scaffold (issue #9): adapter/parser skeleton; closed-loop validation
  pending current mail-list samples from issue #6 (browser collection deferred to an external
  AI).
- Dataset tooling (Wave 1): capture CLI with manifest and SHA-256 evidence hashing, dataset
  utilities, optional `vision` dependency group, and `evo-capture`/`evo-dataset` console
  scripts.
