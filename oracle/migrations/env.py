# SPDX-License-Identifier: Apache-2.0
"""Alembic environment for the Orchard oracle.

Targets the same database the service uses (``settings().db_url`` —
``ORCHARD_ORACLE_DB_URL`` / ``oracle/.env``) and the same ``Base.metadata``,
so ``--autogenerate`` diffs against the live models and ``upgrade`` writes to
the configured DB. ``render_as_batch`` keeps generated migrations
SQLite-friendly (batch ALTER); they run unchanged on Postgres.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the models registers every table on Base.metadata — required
# for autogenerate to see the full schema.
from oracle.app import models  # noqa: F401
from oracle.app.config import settings
from oracle.app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
