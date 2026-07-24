# SPDX-License-Identifier: Apache-2.0
"""Atomic uptime-hour upsert — race-safe create-or-increment."""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from oracle.app import models, seasons  # noqa: E402
from oracle.app.db import Base  # noqa: E402
from oracle.app.routes.readings import _bump_uptime_hour  # noqa: E402

WHEN = datetime(2026, 7, 24, 13, 30, 0, tzinfo=timezone.utc)


def _session():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _count(s):
    return s.execute(select(models.UptimeHour)).scalar_one().reading_count


def test_first_bump_creates_count_one():
    s = _session()
    _bump_uptime_hour(s, "NODE", WHEN)
    s.commit()
    assert _count(s) == 1


def test_repeated_bumps_increment():
    s = _session()
    for _ in range(5):
        _bump_uptime_hour(s, "NODE", WHEN)
    s.commit()
    assert _count(s) == 5


def test_upsert_increments_when_row_preexists_no_crash():
    # Simulate a concurrent creator having already inserted the row: the upsert
    # must increment rather than raise a unique-constraint IntegrityError (the
    # old SELECT-then-INSERT would have 500'd here).
    s = _session()
    bucket = seasons.hour_bucket_for(WHEN)
    s.add(models.UptimeHour(node_id="NODE", hour_utc=bucket, reading_count=7))
    s.commit()
    _bump_uptime_hour(s, "NODE", WHEN)
    s.commit()
    assert _count(s) == 8


def test_different_hours_are_separate_rows():
    s = _session()
    _bump_uptime_hour(s, "NODE", WHEN)
    _bump_uptime_hour(s, "NODE", WHEN.replace(hour=14))
    s.commit()
    assert len(s.execute(select(models.UptimeHour)).scalars().all()) == 2
