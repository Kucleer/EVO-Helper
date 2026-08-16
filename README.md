# EVO-Helper

EVO-Helper is a local, safety-first assistant for scanning configured coordinate ranges, identifying
`bot_` targets, preserving evidence, and dispatching fleets. Dispatches are always real; the design
is such that automation cannot dispatch unless every safety invariant has been satisfied.

## Status

Wave 0 is in progress. The legacy workspace was externally archived before this repository was
initialized. Public domain and port contracts are frozen in `docs/contracts.md`.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m compileall src tests
pytest -q
ruff check src tests
ruff format --check src tests
mypy src
```

Run the persistent local management service after installing dependencies:

```powershell
evo-web
```

Startup applies the bundled SQLite migrations and binds only to `127.0.0.1`.

### Planet reconnaissance email alerts

When an existing attack-report reconciliation, report backfill, or outbound-scout
collection opens a mail titled `你的行星被侦察`, EVO-Helper records its source,
target, time, interception count, and raw body. It does **not** run a separate
mail polling loop. A stored mail fingerprint prevents duplicate delivery.

To enable one-shot SMTP notification, copy the commented values from
[`.env.example`](.env.example) into the local `.env`. For a 126 mailbox, use
`smtp.126.com:465` with SSL and a POP3/SMTP/IMAP client authorization password,
never the web-login password. If SMTP is not configured, alerts are still saved
locally and marked `NOT_CONFIGURED`; they are not retried automatically.

The future web service must bind only to `127.0.0.1`. Opening the UI never starts a run; each run
requires an explicit, idempotent user action.
