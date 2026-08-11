# SPDX-License-Identifier: Apache-2.0
"""The oracle's hour-credit quorum: one reading is not an hour of sensing.

Iteration 1 fixed the 60x overstatement on the DataLayer side with a
30-reading signature quorum. hours_online — the number the settlement runner
pays $JUICE on — still credited an hour for ONE accepted reading, so the new
economics would have inherited exactly the defect the quorum killed. The payer
and the verifier must not price the same day differently.
"""
from __future__ import annotations

import datetime as dt

import pytest

from oracle.app import db, models
from oracle.app.uptime_calc import hours_online_for

NODE = "D8641AD6CAE36977818499469F7E8C49"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL",
                       f"sqlite:///{(tmp_path/'q.db').as_posix()}")
    monkeypatch.delenv("ORCHARD_ORACLE_MIN_READINGS_PER_CREDITED_HOUR",
                       raising=False)
    monkeypatch.delenv("ORCHARD_ORACLE_MIN_SLOTS_PER_CREDITED_HOUR",
                       raising=False)
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    s = db.session_factory()()
    now = dt.datetime.now(dt.timezone.utc)
    s.add(models.Node(node_id=NODE, signing_key_hex="ab" * 32, last_seq=0,
                      registered_at=now))
    s.commit()
    yield s
    s.close()


def _hour(session, bucket: str, count: int):
    session.add(models.UptimeHour(node_id=NODE, hour_utc=bucket,
                                  reading_count=count))
    session.commit()


def _season_bucket(i: int) -> str:
    from oracle.app import seasons
    return seasons.hour_buckets_in_season(74)[i]


def test_the_production_default_matches_the_datalayer_quorum(session):
    """One number for 'what is an hour of sensing', everywhere money is
    computed. Drift here is a silent disagreement about pay."""
    from oracle.app.config import settings
    from orchard_chia.datalayer.schema import MIN_VERIFIED_READINGS_PER_HOUR
    assert settings().min_readings_per_credited_hour == \
        MIN_VERIFIED_READINGS_PER_HOUR == 30


def test_one_reading_no_longer_credits_an_hour(session):
    _hour(session, _season_bucket(0), 1)
    hours, hit = hours_online_for(session, NODE, 74)
    assert hours == 0 and hit == []


def test_a_full_hour_credits(session):
    _hour(session, _season_bucket(0), 60)
    assert hours_online_for(session, NODE, 74)[0] == 1


def test_exactly_the_quorum_credits(session):
    _hour(session, _season_bucket(0), 30)
    assert hours_online_for(session, NODE, 74)[0] == 1


def test_one_short_does_not(session):
    _hour(session, _season_bucket(0), 29)
    assert hours_online_for(session, NODE, 74)[0] == 0


def test_hours_are_judged_independently(session):
    _hour(session, _season_bucket(0), 60)
    _hour(session, _season_bucket(1), 1)
    _hour(session, _season_bucket(2), 45)
    hours, hit = hours_online_for(session, NODE, 74)
    assert hours == 2
    assert _season_bucket(1) not in hit


def test_the_override_floor_is_one(session, monkeypatch):
    """Even a test override cannot credit an hour holding nothing."""
    monkeypatch.setenv("ORCHARD_ORACLE_MIN_READINGS_PER_CREDITED_HOUR", "0")
    from oracle.app.config import reset_settings_for_tests
    reset_settings_for_tests()
    _hour(session, _season_bucket(0), 0)
    assert hours_online_for(session, NODE, 74)[0] == 0


# --- burst defense (slots_mask) ---------------------------------------------

def _hour_with_mask(session, bucket: str, count: int, mask: int):
    session.add(models.UptimeHour(node_id=NODE, hour_utc=bucket,
                                  reading_count=count, slots_mask=mask))
    session.commit()


def test_a_burst_hour_is_not_credited(session):
    """30 readings in two minutes: quorum met, one slot bit set."""
    _hour_with_mask(session, _season_bucket(0), 30, 0b000001)
    assert hours_online_for(session, NODE, 74)[0] == 0


def test_a_spread_hour_is_credited(session):
    """Readings across four ten-minute slots — half an hour of real presence."""
    _hour_with_mask(session, _season_bucket(0), 30, 0b011110)
    assert hours_online_for(session, NODE, 74)[0] == 1


def test_a_legacy_row_without_spread_data_is_exempt(session):
    """slots_mask=0 predates the mask; the rule cannot judge what was never
    recorded."""
    _hour_with_mask(session, _season_bucket(0), 30, 0)
    assert hours_online_for(session, NODE, 74)[0] == 1


def test_spread_alone_is_not_enough_either(session):
    """Six slots but only 6 readings: spanned, but not sensing."""
    _hour_with_mask(session, _season_bucket(0), 6, 0b111111)
    assert hours_online_for(session, NODE, 74)[0] == 0
