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

### Which database the tests run on

`pytest -q` builds its scratch databases through `tests/support/database.py`, which picks the
dialect from one environment variable:

| `EVO_HELPER_TEST_DATABASE_URL` | scratch database |
| --- | --- |
| unset | a SQLite file per test — fast, good enough for local iteration |
| a PostgreSQL URL | one schema per test inside that database — same dialect as production |

CI sets it, so every pull request is checked against PostgreSQL. That matters: on 2026-08-16 the
`/planets` page returned 500 in production because a category count selected a column that was not
in its `GROUP BY` — SQLite tolerates that, PostgreSQL raises `GroupingError`, and all 226 tests
covering the page were green on SQLite.

To run against PostgreSQL locally, point the variable at a **scratch** database (never production)
and install the `db` extra for the driver:

```powershell
python -m pip install -e ".[dev,db]"
$env:EVO_HELPER_TEST_DATABASE_URL = "postgresql+psycopg://user:password@host:5432/evo_helper_test"
pytest -q
python tests/support/database.py   # drop the evotest_* schemas afterwards
```

Run the persistent local management service after installing dependencies:

```powershell
evo-web
```

Startup applies the bundled migrations to whatever `EVO_HELPER_DATABASE_URL` points at.
The live deployment is PostgreSQL — install the `db` extra for the driver; see
[`docs/部署到挂机机器.md`](docs/部署到挂机机器.md). There is no silent SQLite fallback: with no
configuration at all the default URL points at a local PostgreSQL that most likely does not exist,
so a missing `.env` fails loudly instead of quietly opening an empty database.

The service binds `0.0.0.0:8770` by default so other devices on the LAN can open the console —
a deliberate choice, and the reason it is only fit for a trusted network. Set
`EVO_HELPER_HOST=127.0.0.1` to keep it on this machine, and set `EVO_HELPER_WEB_TOKEN`
before letting another machine drive it.

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

Opening the UI never starts a run; each run requires an explicit, idempotent user action.
