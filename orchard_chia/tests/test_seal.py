# SPDX-License-Identifier: Apache-2.0
"""Tests for sealed Season root derivation from published readings."""
from __future__ import annotations

# These fixtures are 1-3 readings because what they pin is season_root
# derivation and signature handling, not how many readings an hour is
# worth. They therefore state min_readings_per_hour=1 rather than
# inheriting the production quorum, so raising that constant cannot
# silently turn a seal-mechanics test into a threshold test. The quorum
# itself is pinned by test_verified_hours_quorum.py and by the golden
# vectors' verified_hours_cases table.

from orchard_chia.datalayer import schema, seal


NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def _signed(ts: int, temp: int = 21000) -> dict:
    return schema.sign_reading(
        {
            "node_id": NODE,
            "ts_ms": ts,
            "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {"temperature_mc": temp, "gps_fix": True, "gps_sats": 5},
        },
        SEED,
    )


def test_seal_from_readings_matches_season_root():
    r0 = _signed(1000)
    r1 = _signed(2000, 22000)
    batch0 = schema.build_readings_batch(
        node_id=NODE, season=5, hour=0, readings=[r0]
    )
    batch1 = schema.build_readings_batch(
        node_id=NODE, season=5, hour=1, readings=[r1]
    )
    out = seal.seal_from_readings(
        [batch0, batch1], device_pubkey=PUB, min_readings_per_hour=1
    )
    assert out is not None
    assert out.source == "readings"
    assert out.hour_count == 2
    assert out.reading_count == 2
    assert out.verified_hours == 2
    expected = schema.season_root(
        {0: batch0["hour_root"], 1: batch1["hour_root"]}
    )
    assert out.season_root == expected


def test_seal_verified_hours_drops_bad_sigs():
    good = _signed(1000)
    bad = {**_signed(2000), "sig": "00" * 64}
    batch = schema.build_readings_batch(
        node_id=NODE, season=3, hour=7, readings=[good, bad]
    )
    # hour still has one valid reading → verified_hours = 1
    out = seal.seal_from_readings([batch], device_pubkey=PUB, min_readings_per_hour=1)
    assert out is not None
    assert out.verified_hours == 1


def test_seal_empty_returns_none():
    assert seal.seal_from_readings([], device_pubkey=PUB, min_readings_per_hour=1) is None


def test_seal_counts_hour_root_mismatch():
    r0 = _signed(1000)
    batch = schema.build_readings_batch(
        node_id=NODE, season=5, hour=0, readings=[r0]
    )
    # Corrupt the stored hour_root so it disagrees with a recompute.
    batch = {**batch, "hour_root": "00" * 32}
    out = seal.seal_from_readings([batch], device_pubkey=PUB, min_readings_per_hour=1)
    assert out is not None
    assert out.root_mismatches == 1
    # The seal still uses the recomputed (correct) root, not the corrupt one.
    assert out.season_root != "00" * 32


def test_seal_no_mismatch_when_root_matches():
    r0 = _signed(1000)
    batch = schema.build_readings_batch(
        node_id=NODE, season=5, hour=0, readings=[r0]
    )
    out = seal.seal_from_readings([batch], device_pubkey=PUB, min_readings_per_hour=1)
    assert out is not None and out.root_mismatches == 0


def test_seal_with_pubkey_marks_sigs_verified():
    batch = schema.build_readings_batch(
        node_id=NODE, season=5, hour=0, readings=[_signed(1000)]
    )
    out = seal.seal_from_readings([batch], device_pubkey=PUB, min_readings_per_hour=1)
    assert out is not None and out.sigs_verified is True


def test_seal_without_pubkey_is_presence_only():
    batch = schema.build_readings_batch(
        node_id=NODE, season=5, hour=0, readings=[_signed(1000)]
    )
    out = seal.seal_from_readings([batch], device_pubkey=None, min_readings_per_hour=1)
    assert out is not None
    assert out.sigs_verified is False
    # Presence count still non-zero, just not signature-verified.
    assert out.verified_hours == 1


def test_load_season_readings_via_fake_rpc():
    r = _signed(1)
    batch = schema.build_readings_batch(
        node_id=NODE, season=2, hour=9, readings=[r]
    )
    store = {
        schema.readings_key(NODE, 2, 9): schema.value_hex(batch),
    }

    class Fake:
        def get_keys(self, store_id):
            return list(store.keys())

        def get_value(self, store_id, key_hex):
            return store.get(key_hex)

    rows = seal.load_season_readings(Fake(), "s", node_id=NODE, season=2)
    assert len(rows) == 1
    assert rows[0]["hour"] == 9
