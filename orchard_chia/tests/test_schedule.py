# SPDX-License-Identifier: Apache-2.0
"""Closed-hour publish scheduling tests."""
from __future__ import annotations

from datetime import datetime, timezone

from orchard_chia.datalayer import schedule


def test_last_closed_hour_mid_hour():
    now = datetime(2026, 6, 1, 13, 45, 30, tzinfo=timezone.utc)
    last = schedule.last_closed_hour_start(now)
    assert last == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_last_closed_hour_on_boundary():
    now = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    last = schedule.last_closed_hour_start(now)
    assert last == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_closed_hour_window_ms():
    start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    ch = schedule.closed_hour_for_start(start)
    assert ch.hour == 12
    assert ch.end_ms - ch.start_ms == 3_600_000
    assert ch.start_ms == int(start.timestamp() * 1000)


def test_iter_closed_hours_oldest_first():
    now = datetime(2026, 6, 1, 15, 10, 0, tzinfo=timezone.utc)
    hours = schedule.iter_closed_hours(lookback_hours=3, now=now)
    assert len(hours) == 3
    assert [h.hour for h in hours] == [12, 13, 14]
    assert hours[0].start_utc < hours[-1].start_utc


def test_season_number_day_aligned():
    # Genesis 2026-05-27 → season 1; 2026-06-01 is day 5 → season 6
    ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert schedule.season_number_for(ts) == 6


def test_is_closed_hour():
    now = datetime(2026, 6, 1, 15, 10, 0, tzinfo=timezone.utc)
    # last closed is 14:00 of season 6
    last = schedule.closed_hour_for_start(schedule.last_closed_hour_start(now))
    assert schedule.is_closed_hour(last.season, last.hour, now=now)
    assert not schedule.is_closed_hour(last.season, last.hour + 1, now=now)


def test_harvest_skips_watermark_and_uses_window():
    from orchard_chia.datalayer import publish

    class FakeOracle:
        def __init__(self):
            self.calls = []

        def get_readings(self, node_id, limit=500, *, since_ms=None, until_ms=None):
            self.calls.append((node_id, since_ms, until_ms, limit))
            return []

    fo = FakeOracle()
    now = datetime(2026, 6, 1, 15, 10, 0, tzinfo=timezone.utc)
    closed = schedule.iter_closed_hours(lookback_hours=2, now=now)
    nid = "AABBCCDDEEFF0011AABBCCDDEEFF0011"
    published = {(nid, closed[0].season, closed[0].hour)}

    batches, notes = publish.harvest_closed_hour_batches(
        fo,
        [{"node_id": nid, "device_pubkey": "02" + "ab" * 32}],
        closed,
        already_published=lambda n, s, h: (n.upper(), s, h) in published,
        current_season=6,
    )
    assert batches == []
    assert any("watermarked" in n for n in notes)
    assert len(fo.calls) == 1
    assert fo.calls[0][1] == closed[1].start_ms
    assert fo.calls[0][2] == closed[1].end_ms
