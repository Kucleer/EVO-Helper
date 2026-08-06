# EVO-Helper

EVO-Helper is a local, safety-first assistant for scanning configured coordinate ranges, identifying
`bot_` targets, preserving evidence, and planning dispatches. It starts in `dry_run=true` mode and is
designed so that browser automation cannot dispatch unless every safety invariant has been satisfied.

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

The future web service must bind only to `127.0.0.1`. Opening the UI never starts a run; each run
requires an explicit, idempotent user action.
