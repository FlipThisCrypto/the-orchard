# SPDX-License-Identifier: Apache-2.0
"""A skipped hour says WHICH kind of nothing it found.

"no device-signed rows" covered three unrelated situations, and a live
catch-up run printing it nine times read as "the firmware has stopped
signing" when the Tree had simply been offline. The operator's next move is a
hardware check, a reflash, or a clock investigation — not the same move.
"""
from __future__ import annotations

from orchard_chia.datalayer.publish import harvest_closed_hour_batches
from datetime import datetime, timezone

from orchard_chia.datalayer.schedule import ClosedHour


class FakeOracle:
    def __init__(self, rows):
        self._rows = rows

    def get_readings(self, node_id, limit=2000, since_ms=None, until_ms=None):
        return self._rows


NODE = "D8641AD6CAE36977818499469F7E8C49"


def _hour():
    return ClosedHour(season=76, hour=13, start_ms=1_000_000, end_ms=4_600_000,
                      start_utc=datetime(1970, 1, 1, 0, 16, tzinfo=timezone.utc))


def _notes(rows):
    _, notes = harvest_closed_hour_batches(
        FakeOracle(rows), [{"node_id": NODE}], [_hour()],
        already_published=lambda n, s, h: False, current_season=77)
    return " ".join(notes)


def test_an_offline_hour_says_offline():
    assert "Tree offline this hour" in _notes([])


def test_unsigned_readings_say_so_and_count_them():
    rows = [{"received_at": "x", "payload": {"sensors": {}}} for _ in range(3)]
    note = _notes(rows)
    assert "NONE device-signed" in note and "3 reading(s)" in note


def test_the_three_reasons_are_distinct():
    """The whole point: one message for three causes sends the operator to the
    wrong investigation."""
    import inspect
    from orchard_chia.datalayer import publish
    src = inspect.getsource(publish)
    for phrase in ("Tree offline this hour", "NONE device-signed",
                   "device clock skew"):
        assert phrase in src
