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
- Dataset tooling (Wave 1): capture CLI with manifest and SHA-256 evidence hashing, dataset
  utilities, optional `vision` dependency group, and `evo-capture`/`evo-dataset` console
  scripts.
- Application integration (issue #14): the safe workflow closing scanning, dry-run dispatch
  recording, and report draining; database-backed range bindings resolving each run's origin
  and preset signature; the SQLite-backed Web service and persistent application factory; and
  the `evo-web` command, which applies Alembic migrations before serving.
- Evidence stores (issue #6): artifact persistence with SHA-256 indexing, UI-observation
  records, strict live-capture metadata validation, and a fail-closed session-recovery wrapper
  for the logged-in entry page.
- Live report reading (issues #9, #10): measured report ROI geometry for the
  `evo-20260807-live` batch, an `ImageReportScreens` adapter cropping those ROIs into
  Tesseract, and a reader chaining mail list to attack report to battle replay.
- Report ingestion (issue #13): screenshots to OCR to domain records to SQLite to the Web UI,
  with one UI observation recorded per screen so no single version label stands for the chain.
- Code-driven capture (issue #13): single-window capture via `PrintWindow` with an `mss`
  fallback, cropped to the client area — never a full-screen grab, which would pick up
  unrelated windows. Verified to reach Chrome's WebGL canvas, including off-screen and
  unfocused.
- Humanised input (issue #13): randomised click offset, travel time, and pacing for capture
  navigation, refusing any dispatch/claim/delete label and requiring `pyautogui.FAILSAFE`.
- Intel search (issue #18): coordinate-range plus condition-tree filtering (fleet total and
  per-ship-type, AND/OR nesting) with cursor pagination and sorting, all server-side, plus
  persisted named filters.
- Local operations console (issue #18): a dark two-section console (mission centre, intel
  centre) with run detail and diagnostics as auxiliary pages. Status is never carried by
  colour alone, and `dry_run` is displayed as a lock with no toggle.

### Changed

- Capture manifests conform to the dataset integrity contract and require explicit baseline
  eligibility; the runtime parser rejects legacy mail-list captures, and browser capture,
  reconnect, and per-screen UI-version gates are documented (issue #6).
- Plan ranges require the expected fleet-preset signature, and persisted plans carry a stable
  public UUID and an auditable update timestamp (issue #17).
- Report parsers rebuilt against the 2026-08-07 live layout; the unit catalogue now matches the
  in-game list (18 ships, 11 defences, in game order), correcting earlier guesses
  (`运输舰` to `小型运输船`, `间谍探测器` to `探测器`) and adding `收割者`, `湮灭之星`, missiles,
  and shields (issues #10, #18).
- Legacy pages redirect into the console: `/` and `/plans` to the mission centre, `/targets` to
  the intel centre (issue #18).

### Fixed

- Game times are read as UTC+0, the zone the game renders in. A bare timestamp was previously
  parsed in the UTC+8 schedule zone, shifting every report by eight hours and breaking the
  strict origin/target/time match against a dispatch (issue #10).
- Ship names are snapped to the known unit vocabulary. Two OCR passes are combined — names from
  `chi_sim`, counts from `chi_sim+eng` — because neither reads both correctly. A garbled name
  made every report look like a first sighting, since the name is the fleet-timeline diff key
  (issue #18).
- The scan-range origin is no longer required to lie inside the range. It is the player's own
  planet and normally sits outside it, so real coordinates were rejected. The rule existed in
  both the fake and persistent services (issue #18).
- Capacity retries are auditable, successful report draining reaches `COMPLETED`, and a failed
  report-page navigation pauses the run; workflow outcomes synchronise the persisted run
  aggregate (issue #14).

### Security

- ActionGuard re-verifies the required attack screen immediately before consuming a dispatch
  token, so a high-confidence but wrong screen cannot authorise the final click (issue #15).
- Unknown attack UI and exhausted capacity halt before any attack intent or dispatch record is
  created (issue #15).
- Dry-run dispatch records cannot close battle reports, and cursor recovery is proven by a
  restart-level SQLite workflow test (issue #14).
