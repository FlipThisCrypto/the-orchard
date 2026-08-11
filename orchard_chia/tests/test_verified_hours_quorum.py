# SPDX-License-Identifier: Apache-2.0
"""An hour of sensing has to contain an hour of sensing.

``verified_hours`` used to count any hour holding at least ONE signature-valid
reading. Firmware samples every 60 s, so a healthy hour holds about sixty. A
Tree reporting once an hour therefore earned exactly what a Tree reporting
continuously earned — a 60x overstatement of the quantity the field names —
and ``payout/calculator.py`` turns that number straight into tokens.

Every signature involved was genuine. That is what made it hard to see: nothing
was forged, the arithmetic was right, and the word "verified" was doing work it
had not earned.

These tests pin the rule and, just as importantly, pin that the rule travels
INSIDE the signed record. A threshold a verifier has to guess is a threshold
that changes retroactively the moment this module does.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import schema, seal

NODE = "AABBCCDDEEFF0011AABBCCDDEEFF0011"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)
QUORUM = schema.MIN_VERIFIED_READINGS_PER_HOUR


def _reading(ts_ms: int) -> dict:
    return schema.sign_reading({
        "node_id": NODE, "ts_ms": ts_ms, "block_anchor": "a" * 16,
        "metrics": {"temperature_mc": 20000},
    }, SEED)


def _hour(n: int, start: int = 1_000_000) -> list[dict]:
    return [_reading(start + i * 60_000) for i in range(n)]


# --- the rule ---------------------------------------------------------------

def test_one_reading_no_longer_buys_an_hour():
    """The defect, stated directly."""
    assert schema.verified_hours({0: _hour(1)}, PUB) == 0


def test_a_full_hour_counts():
    assert schema.verified_hours({0: _hour(60)}, PUB) == 1


def test_exactly_the_quorum_counts():
    assert schema.verified_hours({0: _hour(QUORUM)}, PUB) == 1


def test_one_short_of_the_quorum_does_not():
    assert schema.verified_hours({0: _hour(QUORUM - 1)}, PUB) == 0


def test_the_quorum_is_a_meaningful_fraction_of_the_cadence():
    """A threshold of 1 or 2 would leave the defect essentially in place.

    Firmware's ORCHARD_SAMPLE_INTERVAL_MS is 60000 (firmware/include/config.h),
    so ~60 readings an hour. Pinned as a range rather than a number so tuning
    stays possible without the value silently drifting back toward 1.
    """
    assert 15 <= QUORUM <= 60, (
        f"a quorum of {QUORUM} against a ~60/hour cadence is not a quorum"
    )


def test_hours_are_judged_independently():
    got = schema.verified_hours({0: _hour(QUORUM), 1: _hour(1), 2: _hour(60)}, PUB)
    assert got == 2, "the thin hour is dropped, the full ones are not"


def test_invalid_signatures_do_not_count_toward_the_quorum():
    """Otherwise padding an hour with junk would buy it back."""
    good = _hour(QUORUM - 1)
    junk = [dict(r, sig="00" * 64) for r in _hour(50, start=9_000_000)]
    assert schema.verified_hours({0: good + junk}, PUB) == 0


def test_readings_signed_by_another_key_do_not_count():
    other = schema.pubkey_for_seed("02" + "00" * 31)
    assert schema.verified_hours({0: _hour(60)}, other) == 0


def test_an_empty_hour_is_worth_nothing():
    assert schema.verified_hours({0: []}, PUB) == 0


def test_a_zero_threshold_is_refused():
    """It would credit an hour containing no readings at all."""
    with pytest.raises(ValueError, match="at least 1"):
        schema.verified_hours({0: []}, PUB, min_readings=0)


# --- the rule travels with the record ---------------------------------------

def test_the_attest_record_declares_the_rule_it_was_judged_by():
    body = schema.build_attest(
        node_id=NODE, season=5, season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z", hours_online=1, verified_hrs=1,
        reading_count=60, block_height_at_write=1, season_root_hex="ab" * 32,
        signed_at="2026-06-01T00:05:00Z",
    )
    assert body["min_readings_per_hour"] == QUORUM


def test_the_declaration_is_inside_the_signature():
    """If it were outside, anyone could restate the rule after the fact and
    make an overstatement verify."""
    body = schema.build_attest(
        node_id=NODE, season=5, season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z", hours_online=1, verified_hrs=1,
        reading_count=60, block_height_at_write=1, season_root_hex="ab" * 32,
        signed_at="2026-06-01T00:05:00Z", min_readings_per_hour=1,
    )
    signed = schema.sign_attest(body, SEED)
    tampered = dict(signed, min_readings_per_hour=999)
    pub = schema.pubkey_for_seed(SEED)
    assert schema.verify_attest(signed, pub) is True
    assert schema.verify_attest(tampered, pub) is False, (
        "restating the threshold must break the signature"
    )


def test_a_seal_carries_the_threshold_it_used():
    batches = [schema.build_readings_batch(
        node_id=NODE, season=2, hour=0, readings=_hour(QUORUM))]
    out = seal.seal_from_readings(batches, device_pubkey=PUB)
    assert out is not None
    assert out.min_readings_per_hour == QUORUM
    assert out.verified_hours == 1


def test_a_thin_hour_seals_to_zero_verified_hours():
    batches = [schema.build_readings_batch(
        node_id=NODE, season=2, hour=0, readings=_hour(1))]
    out = seal.seal_from_readings(batches, device_pubkey=PUB)
    assert out is not None, "the hour still exists and still roots"
    assert out.verified_hours == 0, "it just is not worth an hour"


def test_presence_counting_without_a_pubkey_also_respects_the_quorum():
    """The no-pubkey path counts presence rather than signatures, but a thin
    hour is a thin hour either way."""
    thin = [schema.build_readings_batch(
        node_id=NODE, season=2, hour=0, readings=_hour(1))]
    out = seal.seal_from_readings(thin, device_pubkey=None)
    assert out is not None and out.verified_hours == 0
    assert out.sigs_verified is False


# --- records written under the old rule stay honest -------------------------

def test_a_legacy_record_is_judged_by_the_rule_it_declared():
    """Raising the bar must not retroactively brand older records as lies.

    A record signed when "≥1" was the rule declared min_readings_per_hour=1 (or
    nothing at all). Re-verifying it under today's constant would report an
    overstatement that its writer never made.
    """
    readings = _hour(1)
    batch = schema.build_readings_batch(node_id=NODE, season=5, hour=13,
                                        readings=readings)
    sr = schema.season_root({13: batch["hour_root"]})
    attest = schema.sign_attest(schema.build_attest(
        node_id=NODE, season=5, season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z", hours_online=1, verified_hrs=1,
        reading_count=1, block_height_at_write=1, season_root_hex=sr,
        signed_at="2026-06-01T00:05:00Z", min_readings_per_hour=1,
    ), SEED)

    by_hour = {13: readings}
    declared = attest["min_readings_per_hour"]
    assert schema.verified_hours(by_hour, PUB, min_readings=declared) == 1
    assert schema.verified_hours(by_hour, PUB) == 0, (
        "and under today's rule the same evidence is worth nothing — which is "
        "why the record has to say which rule it was written under"
    )


def test_a_record_declaring_nothing_falls_back_to_the_legacy_rule():
    assert schema.LEGACY_MIN_READINGS_PER_HOUR == 1
