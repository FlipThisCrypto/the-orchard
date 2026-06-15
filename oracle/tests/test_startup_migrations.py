# SPDX-License-Identifier: Apache-2.0
"""The create_all() bootstrap must additively migrate pre-existing tables.

The production oracle applies schema with db.create_all() + idempotent ALTERs
(not Alembic upgrade). create_all() only CREATES missing tables — it never adds
a column to an existing one — so each new column on an existing table needs an
ALTER here, or the updated code crashes on the live DB. This pins that for the
T14 `readings.schema_version` column (the same class of bug `last_seq` had).
"""
from __future__ import annotations

import os

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

from sqlalchemy import inspect

from oracle.app import db as dbmod
from oracle.app.config import reset_settings_for_tests
from oracle.app.db import reset_for_tests


def test_create_all_adds_schema_version_to_existing_readings(tmp_path, monkeypatch):
    db_file = tmp_path / "pre_t14.db"
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{db_file.as_posix()}")
    reset_settings_for_tests()
    reset_for_tests()

    eng = dbmod.engine()
    # Simulate a pre-T14 readings table — no schema_version column.
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE readings (id INTEGER PRIMARY KEY, node_id VARCHAR(64), "
            "payload_json TEXT, sig_hex VARCHAR(64))")

    dbmod.create_all()  # must ALTER the column in (and create the other tables)

    cols = {col["name"] for col in inspect(eng).get_columns("readings")}
    assert "schema_version" in cols
    # Idempotent: a second pass is a no-op, not an error.
    dbmod.create_all()


def test_migrate_helper_is_idempotent_and_noop_without_table(tmp_path, monkeypatch):
    db_file = tmp_path / "empty.db"
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{db_file.as_posix()}")
    reset_settings_for_tests()
    reset_for_tests()
    eng = dbmod.engine()
    # No readings table yet -> helper returns quietly (create_all makes it later).
    dbmod._migrate_reading_schema_version_column(eng)
    assert "readings" not in inspect(eng).get_table_names()
