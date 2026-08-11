"""Alembic environment wired to the EVO-Helper SQLAlchemy models."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from evo_helper.storage import models  # noqa: F401  # register tables on Base.metadata
from evo_helper.storage.database import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every
    # logger already configured by the application. The web runtime applies
    # migrations at startup, so that would kill the report-timing log for the
    # rest of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = config.attributes.get("database_url") or os.environ.get("EVO_HELPER_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live connection from the configured engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
