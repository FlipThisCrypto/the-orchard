# SPDX-License-Identifier: Apache-2.0
"""The attest writer must be idempotent for a closed Season.

Regression: signed_at was datetime.now(), and it lives INSIDE the signed body —
so value_hex differed on every run, the `existing_hex == value_hex` skip was
unreachable, and each daily run delete+inserted every attestation for every past
season (unbounded, fee-bearing, and rewriting historical seal times).
"""
from __future__ import annotations

from orchard_chia.datalayer import schema
from orchard_chia.datalayer.main import _season_seal_time

UPTIME = {
    "season_start_utc": "2026-05-31T00:00:00+00:00",
    "season_end_utc": "2026-06-01T00:00:00+00:00",
}
SEED = "01" + "00" * 31


def _sign(uptime):
    return schema.value_hex(schema.sign_attest(
        schema.build_attest(
            node_id="AB" * 16, season=5,
            season_start_utc=uptime["season_start_utc"],
            season_end_utc=uptime["season_end_utc"],
            hours_online=24, verified_hrs=24, reading_count=10,
            block_height_at_write=1,
            season_root_hex="ab" * 32,
            signed_at=_season_seal_time(uptime),
        ),
        SEED,
    ))


def test_reseal_of_a_closed_season_is_byte_identical():
    assert _sign(UPTIME) == _sign(UPTIME), (
        "re-running the writer must reproduce identical bytes, or the "
        "unchanged-skip is dead and every run rewrites every attestation"
    )


def test_seal_time_is_the_season_boundary_not_now():
    assert _season_seal_time(UPTIME) == "2026-06-01T00:00:00Z"


def test_seal_time_normalizes_z_and_offset_forms():
    assert _season_seal_time({"season_end_utc": "2026-06-01T00:00:00Z"}) == \
        _season_seal_time({"season_end_utc": "2026-06-01T00:00:00+00:00"})


def test_seal_time_is_defensive_about_bad_input():
    # Never falls back to now() (which would silently restore the defect).
    assert _season_seal_time({}) == "1970-01-01T00:00:00Z"
    assert _season_seal_time({"season_end_utc": "not-a-date"}) == "not-a-date"


def test_a_changed_fact_still_produces_a_new_record():
    """Idempotency must not mean 'never updates'."""
    a = _sign(UPTIME)
    other = {**UPTIME, "season_end_utc": "2026-06-02T00:00:00+00:00"}
    assert _sign(other) != a
