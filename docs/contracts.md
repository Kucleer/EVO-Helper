# Frozen contracts (CP2)

## Time and identifiers

- Persist business timestamps as timezone-aware UTC `datetime` values.
- Schedule evaluation uses `Asia/Shanghai` (UTC+8); presentation of business events remains UTC.
- A manual start request requires a caller-supplied `idempotency_key`.
- Coordinates are `(galaxy, system, position)` positive integer triples and range boundaries are inclusive.

## Domain states

`DRAFT -> ARMED -> SCANNING -> WAITING_CAPACITY -> SCANNING -> DRAINING -> COMPLETED` is the
normal path. Every active state can transition to `PAUSED`, `FAILED`, or `EMERGENCY_STOPPED`.
Only application services may transition state and must append a `state_events` record.

## Ports

The canonical Protocol definitions live in `evo_helper.domain.ports`: `GamePort`, `RepositoryPort`,
`ClockPort`, and `ArtifactPort`. Infrastructure, vision, game, storage, and web adapters depend on
those contracts; the domain package does not depend on their frameworks.

## Safety boundary

`DispatchCommand` is a declarative request only. No frozen port or domain type performs a click.
An adapter may execute a final dispatch only after an application-level ActionGuard decision; the
default setting is always `dry_run=true`.

## API baseline

The first API surface is: plans CRUD, manual run start, pause/resume/emergency-stop, run status,
targets/history, revisits, and diagnostics. Mutating endpoints require same-origin CSRF protection or
an equivalent local token. The service must bind to loopback only.
