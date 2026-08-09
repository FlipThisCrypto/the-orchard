# SPDX-License-Identifier: Apache-2.0
"""attest must not search Seasons a Tree could not have existed in.

With ``max_lookback_seasons: null`` the writer walked Season 1..current for
every Tree — 73 x 6 = 438 oracle round trips on the live fleet, 72 Seasons of
which predate every Tree in the network. The run could not finish, and the waste
grows by one Season per day, forever.

A config number would need re-tuning forever. The Tree's own registration date
is self-tuning: a Tree registered today is searched from today.

The direction of failure matters. An unknown or unparseable registration date
must WIDEN the search back to the floor, never narrow it — skipping a Season a
Tree really was online in would silently lose a sealed attestation, and a
missing attestation is indistinguishable from a Tree that was never up.
"""
from __future__ import annotations

from orchard_chia.datalayer import schedule
from orchard_chia.datalayer.main import _first_plausible_season as first_season


def test_a_tree_registered_today_is_not_searched_back_to_season_one():
    # The real case: D8641AD6 registered 2026-08-08, genesis 2026-05-27.
    n = {"registered_at": "2026-08-08T02:47:12.061647"}
    assert first_season(n) == schedule.season_number_for(
        __import__("datetime").datetime.fromisoformat("2026-08-08T02:47:12.061647+00:00")
    )
    assert first_season(n) > 70, "a Tree registered in Season 74 must not be searched from 1"


def test_an_older_tree_is_searched_from_its_own_registration():
    n = {"registered_at": "2026-06-16T22:08:52.083860"}
    assert first_season(n) == 21


def test_first_seen_utc_is_accepted_too():
    # node: cards carry first_seen_utc; /nodes carries registered_at.
    assert first_season({"first_seen_utc": "2026-06-16T22:08:52"}) == 21


def test_a_trailing_z_is_understood():
    assert first_season({"registered_at": "2026-06-16T22:08:52Z"}) == 21


def test_a_naive_timestamp_is_read_as_utc():
    # The oracle serves offset-less timestamps. Reading them as local time is a
    # mistake this project has made before and paid for.
    assert first_season({"registered_at": "2026-06-16T22:08:52"}) == \
           first_season({"registered_at": "2026-06-16T22:08:52+00:00"})


def test_unknown_dates_widen_rather_than_narrow():
    """The safety property: never skip a Season on a guess."""
    for bad in (None, "", "   ", "not-a-date", 12345, {"nested": 1}, "2026-13-45T99:99:99"):
        assert first_season({"registered_at": bad}) == 1, (
            f"{bad!r} must fall back to the floor, not skip Seasons"
        )
    assert first_season({}) == 1


def test_the_configured_floor_is_still_respected():
    # max_lookback_seasons sets a floor; a Tree older than it must not drag the
    # search back past what the operator asked for.
    n = {"registered_at": "2026-06-16T22:08:52"}      # season 21
    assert first_season(n, floor=60) == 60
    # …and a Tree NEWER than the floor still starts at its own registration.
    assert first_season({"registered_at": "2026-08-08T02:47:12"}, floor=60) > 60


def test_the_floor_never_goes_below_one():
    assert first_season({"registered_at": "1999-01-01T00:00:00"}) >= 1
