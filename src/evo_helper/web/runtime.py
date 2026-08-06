"""Runnable, loopback-only persistent EVO-Helper Web service."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from evo_helper.config import Settings
from evo_helper.storage.database import create_database_engine, create_session_factory

from .app import create_persistent_app


def create_runtime_app(
    settings: Settings | None = None, *, local_token: str | None = None
) -> FastAPI:
    """Apply schema migrations and build the real local management service."""
    actual_settings = settings or Settings()
    _upgrade_database(actual_settings.database_url)
    engine = create_database_engine(actual_settings.database_url)
    app = create_persistent_app(
        create_session_factory(engine), settings=actual_settings, local_token=local_token
    )
    app.state.database_engine = engine
    return app


def main() -> int:
    """Start the production local service; Settings enforces loopback binding."""
    settings = Settings()
    uvicorn.run(create_runtime_app(settings), host=settings.host, port=settings.port)
    return 0


def _upgrade_database(database_url: str) -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
