# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy engine + Session factory.

All FastAPI routes obtain a Session via the `get_db` dependency.

Schema bootstrap: ``create_all()`` runs on app startup (see main.py) and is
the zero-config path for dev/tests — it creates missing tables and applies
the idempotent additive ALTERs below. Versioned, Postgres-ready migrations
live in ``oracle/migrations`` (Alembic, T4); see that dir's README for the
``stamp`` / ``upgrade`` workflow. The two agree: the Alembic baseline builds
the same schema ``create_all()`` does (asserted in tests/test_migrations.py).
"""
from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


_engine = None
_session_factory: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        url = settings().db_url
        # SQLite + the default check_same_thread guard makes FastAPI's
        # threadpool unhappy — turn it off for SQLite specifically.
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        # On-disk SQLite: WAL for durable concurrent reads while the writer
        # commits (the dashboard + Season writer read while the oracle
        # writes), plus a busy timeout so a brief writer lock waits instead
        # of erroring. Skipped for :memory: (no WAL there) and for non-SQLite
        # (Postgres handles concurrency natively — T4 / D4 Postgres path).
        if url.startswith("sqlite") and ":memory:" not in url:
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=10000")
                cur.close()
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=engine(), autoflush=False, autocommit=False)
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — one DB session per request, always closed."""
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def _sqlite_path_from_url(url: str) -> Path | None:
    """Return the on-disk SQLite path from a sqlite:/// URL, or None
    if the URL isn't sqlite-on-disk."""
    if not url.startswith("sqlite:///"):
        return None
    raw = url.replace("sqlite:///", "", 1)
    if raw in ("", ":memory:"):
        return None
    return Path(raw)


def _restrict_db_file_perms(db_path: Path) -> None:
    """Make the SQLite file owner-readable-only (0600).

    The DB stores per-Tree HMAC ``signing_key_hex`` in plaintext. On a
    multi-user POSIX host, default umask would leave it group/other-
    readable. We tighten to 0600 every startup; the chmod is idempotent
    and survives DB recreates.

    On Windows ``os.chmod`` only flips the read-only bit — it does
    NOT translate to a real ACL change. NTFS inherits ACLs from the
    parent directory, so the real protection on Windows is "don't put
    the oracle data dir somewhere group-readable" (e.g. keep it under
    your user profile). We log a one-line note on Windows so the
    operator knows the chmod was a no-op there.
    """
    try:
        os.chmod(db_path, 0o600)
    except OSError as e:
        # Don't crash startup if the FS doesn't support chmod (e.g.
        # FAT32 on a removable drive). Log loudly so the operator
        # knows the DB is not file-perm-protected.
        print(f"[oracle.db] WARN: could not chmod {db_path} to 0600: {e}",
              file=sys.stderr)
        return
    if sys.platform == "win32":
        print(f"[oracle.db] note: chmod 0600 on Windows only sets the "
              f"read-only bit. DB at {db_path} is protected by its "
              f"directory's NTFS ACL. Keep oracle/data under your user "
              f"profile.", file=sys.stderr)


def _migrate_node_pass_columns(eng) -> None:
    """Add Phase 6.5's pass_nft_id and pass_verified_at columns to an
    existing `nodes` table that pre-dates them.

    SQLAlchemy's ``create_all()`` is creates-only — it won't add columns
    to a table that already exists, so an oracle DB started before
    Phase 6.5 keeps the old shape and every register call would crash
    on the missing column. Alembic is the right long-term answer; for
    the v1 PoC we just check inspector output and emit a single
    additive ALTER TABLE per missing column. Both columns are nullable
    so the migration is safe regardless of existing data.
    """
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    if "nodes" not in insp.get_table_names():
        return  # fresh DB — create_all() will use the new shape directly.
    existing_cols = {c["name"] for c in insp.get_columns("nodes")}
    additions: list[str] = []
    if "pass_nft_id" not in existing_cols:
        additions.append("ALTER TABLE nodes ADD COLUMN pass_nft_id VARCHAR(128)")
    if "pass_verified_at" not in existing_cols:
        additions.append("ALTER TABLE nodes ADD COLUMN pass_verified_at DATETIME")
    if not additions:
        return
    with eng.begin() as conn:
        for stmt in additions:
            conn.execute(text(stmt))


def _migrate_attestation_chain_columns(eng) -> None:
    """Phase 5.5: add dl_tx_id, dl_key_hex, block_height_at_write to
    `attestations`. Same idempotent-ALTER pattern as the node Pass
    columns. Tracks on-chain identity of each batch_update result so
    the dashboard's "On chain" card has a tx_id + key to render."""
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    if "attestations" not in insp.get_table_names():
        return
    existing_cols = {c["name"] for c in insp.get_columns("attestations")}
    additions: list[str] = []
    if "dl_tx_id" not in existing_cols:
        additions.append("ALTER TABLE attestations ADD COLUMN dl_tx_id VARCHAR(128)")
    if "dl_key_hex" not in existing_cols:
        additions.append("ALTER TABLE attestations ADD COLUMN dl_key_hex VARCHAR(256)")
    if "block_height_at_write" not in existing_cols:
        additions.append("ALTER TABLE attestations ADD COLUMN block_height_at_write INTEGER")
    if not additions:
        return
    with eng.begin() as conn:
        for stmt in additions:
            conn.execute(text(stmt))


def _migrate_node_last_seq_column(eng) -> None:
    """T3 replay protection: add `last_seq` to an existing `nodes` table.

    Same idempotent-ALTER pattern as the Pass/attestation columns. Without
    this, an oracle that gained the `Node.last_seq` model column would crash
    every query against a DB created before the column existed
    (``no such column: nodes.last_seq``). NOT NULL DEFAULT 0 is safe for
    existing rows. Alembic (T4) will supersede these hand-rolled ALTERs.
    """
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    if "nodes" not in insp.get_table_names():
        return
    existing_cols = {c["name"] for c in insp.get_columns("nodes")}
    if "last_seq" in existing_cols:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE nodes ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0"))


def _migrate_reading_schema_version_column(eng) -> None:
    """T14: add `schema_version` to an existing `readings` table.

    Same idempotent-ALTER pattern. `create_all()` only creates missing
    *tables*, never adds a column to an existing one — so an oracle updated
    to the T14 code would otherwise crash on the first /readings against a DB
    created before the column (``no such column: readings.schema_version``).
    Nullable, so existing rows are fine. (Alembic carries the same change for
    migration-managed deployments; this covers the create_all bootstrap.)
    """
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    if "readings" not in insp.get_table_names():
        return
    existing_cols = {c["name"] for c in insp.get_columns("readings")}
    if "schema_version" in existing_cols:
        return
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE readings ADD COLUMN schema_version INTEGER"))


def create_all() -> None:
    """Called once on app startup. Idempotent."""
    eng = engine()
    # Pre-existing tables get additive migrations before create_all
    # has a chance to no-op the new columns into existence.
    _migrate_node_pass_columns(eng)
    _migrate_attestation_chain_columns(eng)
    _migrate_node_last_seq_column(eng)
    _migrate_reading_schema_version_column(eng)
    Base.metadata.create_all(eng)
    # Tighten file perms AFTER the DB file exists. SQLAlchemy creates
    # the file on first connection inside create_all().
    db_path = _sqlite_path_from_url(settings().db_url)
    if db_path is not None and db_path.exists():
        _restrict_db_file_perms(db_path)


def reset_for_tests() -> None:
    """Tests using a different DB URL should call this to reset the engine."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    # Keep process counters from bleeding across test cases.
    from . import metrics

    metrics.reset_for_tests()
