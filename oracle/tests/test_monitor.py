# SPDX-License-Identifier: Apache-2.0
"""Tests for the offline-Tree monitor (T13)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oracle.app import models
from oracle.app.config import reset_settings_for_tests
from oracle.app.db import Base, reset_for_tests
from oracle.app.monitor import find_offline


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")
    reset_settings_for_tests()
    reset_for_tests()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _node(s, nid, last):
    s.add(models.Node(node_id=nid, signing_key_hex="00" * 32, last_reading_at=last))


def test_find_offline_partitions_correctly(session):
    now = datetime.now(timezone.utc)
    with session() as s:
        _node(s, "AA" * 16, now - timedelta(minutes=5))    # fresh -> ok
        _node(s, "BB" * 16, now - timedelta(hours=3))      # silent -> offline
        _node(s, "CC" * 16, now - timedelta(hours=26))     # very silent -> offline
        _node(s, "DD" * 16, None)                          # never reported
        s.commit()
        rep = find_offline(s, timedelta(minutes=120))

    offline_ids = [nid for nid, _, _ in rep.offline]
    assert set(offline_ids) == {"BB" * 16, "CC" * 16}
    assert rep.offline[0][0] == "CC" * 16  # most-silent first
    assert rep.never_reported == ["DD" * 16]


def test_threshold_is_respected(session):
    now = datetime.now(timezone.utc)
    with session() as s:
        _node(s, "EE" * 16, now - timedelta(minutes=90))
        s.commit()
        # 90m silent: offline at a 60m threshold, fine at 120m.
        assert len(find_offline(s, timedelta(minutes=60)).offline) == 1
        assert len(find_offline(s, timedelta(minutes=120)).offline) == 0
