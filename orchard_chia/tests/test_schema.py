# SPDX-License-Identifier: Apache-2.0
"""Tests for orchard_chia.datalayer.schema (the publish-schema reference impl).

Hermetic. Validates against the committed golden vectors so the implementation
can never silently drift from the cross-language contract the firmware and the
public verifier also test against. Covers: canonicalization, key encoding,
ed25519 device + oracle signatures (deterministic), reading leaves / hour root /
season root, the verified-uptime → score math, record builders, and value
round-trips.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchard_chia.datalayer import schema

VEC = json.loads(
    (Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json")
    .read_text(encoding="utf-8")
)

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
DEVICE_SEED = VEC["device"]["seed"]
DEVICE_PUB = VEC["device"]["pubkey"]
ORACLE_SEED = VEC["oracle"]["seed"]
ORACLE_PUB = VEC["oracle"]["pubkey"]


# --- canonicalization ------------------------------------------------------ #
@pytest.mark.parametrize("ex", VEC["canonical_examples"])
def test_canonical_bytes_matches_vector(ex):
    assert schema.canonical_bytes(ex["obj"]).decode("utf-8") == ex["canonical"]


# --- keys ------------------------------------------------------------------ #
def test_keys_match_vectors():
    assert schema.meta_key() == VEC["keys"]["meta"]
    assert schema.node_key(NODE) == VEC["keys"]["node"]
    assert schema.readings_key(NODE, 5, 13) == VEC["keys"]["readings"]
    assert schema.attest_key(NODE, 5) == VEC["keys"]["attest"]
    assert schema.latest_key(NODE) == VEC["keys"]["latest"]


def test_keys_decode_to_ascii():
    assert bytes.fromhex(VEC["keys"]["readings"]).decode() == f"readings:{NODE}:00000005:13"
    assert bytes.fromhex(VEC["keys"]["attest"]).decode() == f"attest:{NODE}:00000005"


def test_node_id_normalized_in_keys():
    assert schema.node_key(NODE.lower()) == schema.node_key(NODE)
    assert schema.readings_key(NODE.lower(), 5, 13) == VEC["keys"]["readings"]


# --- ed25519 keys & signatures --------------------------------------------- #
def test_pubkey_derivation_is_deterministic():
    assert schema.pubkey_for_seed(DEVICE_SEED) == DEVICE_PUB
    assert schema.pubkey_for_seed(ORACLE_SEED) == ORACLE_PUB


def test_device_signing_reproduces_vectors():
    # ed25519 is deterministic: re-signing each body yields the stored sig.
    for stored in VEC["readings_signed"]:
        body = {k: v for k, v in stored.items() if k != "sig"}
        assert schema.sign_reading(body, DEVICE_SEED)["sig"] == stored["sig"]


def test_verify_reading_accepts_genuine():
    for stored in VEC["readings_signed"]:
        assert schema.verify_reading(stored, DEVICE_PUB) is True


def test_verify_reading_detects_tamper():
    stored = dict(VEC["readings_signed"][0])
    stored["ts_ms"] = stored["ts_ms"] + 1
    assert schema.verify_reading(stored, DEVICE_PUB) is False


def test_verify_reading_detects_metric_tamper():
    stored = dict(VEC["readings_signed"][0])
    stored["metrics"] = {**stored["metrics"], "temperature_mc": 99999}
    assert schema.verify_reading(stored, DEVICE_PUB) is False


def test_verify_reading_rejects_wrong_key():
    assert schema.verify_reading(VEC["readings_signed"][0], ORACLE_PUB) is False


def test_verify_reading_rejects_missing_sig():
    body = {k: v for k, v in VEC["readings_signed"][0].items() if k != "sig"}
    assert schema.verify_reading(body, DEVICE_PUB) is False


# --- no-signed-floats invariant (SPEC §0) ---------------------------------- #
def test_sign_reading_rejects_floats():
    body = {"node_id": NODE, "ts_ms": 1, "metrics": {"temperature_mc": 21.4}}
    with pytest.raises(ValueError):
        schema.sign_reading(body, DEVICE_SEED)


def test_reject_floats_allows_ints_and_bools():
    # bool is JSON-stable and not a float; nested ints/lists are fine.
    schema.reject_floats({"a": 1, "b": True, "c": [1, 2], "d": {"e": 3}})


def test_every_vector_metric_is_integer_typed():
    for r in VEC["readings_signed"]:
        for key, val in r["metrics"].items():
            assert not isinstance(val, float), f"{key} is a float"


# --- leaves / roots -------------------------------------------------------- #
def test_reading_leaves_match_vectors():
    ordered = schema._sorted_readings(VEC["readings_signed"])
    assert [schema.reading_leaf(r).hex() for r in ordered] == VEC["reading_leaves"]


def test_hour_root_matches_vector():
    assert schema.hour_root(VEC["readings_signed"]) == VEC["hour_root"]


def test_hour_root_is_order_independent():
    shuffled = list(reversed(VEC["readings_signed"]))
    assert schema.hour_root(shuffled) == VEC["hour_root"]


def test_season_root_matches_vector():
    assert schema.season_root({13: VEC["hour_root"]}) == VEC["season_root"]


def test_single_hour_season_root_equals_hour_root():
    assert schema.season_root({13: VEC["hour_root"]}) == VEC["hour_root"]


# --- verified uptime & score (the tenet) ----------------------------------- #
def test_verified_hours_matches_vector():
    by_hour = {13: VEC["readings_signed"]}
    assert schema.verified_hours(by_hour, DEVICE_PUB) == VEC["verified_hours"]


def test_verified_hours_ignores_bad_signatures():
    # Readings that don't verify don't count the hour.
    by_hour = {13: VEC["readings_signed"]}
    assert schema.verified_hours(by_hour, ORACLE_PUB) == 0


@pytest.mark.parametrize(
    "v,expected",
    [(0, 0), (1, 4), (3, 13), (6, 25), (9, 38), (12, 50), (18, 75), (24, 100)],
)
def test_season_score_round_half_up(v, expected):
    assert schema.season_score(v) == expected


# --- record builders / round-trips ----------------------------------------- #
def test_readings_batch_matches_vector():
    batch = schema.build_readings_batch(
        node_id=NODE, season=5, hour=13, readings=VEC["readings_signed"]
    )
    assert batch == VEC["records"]["readings"]


def test_attest_build_and_sign_reproduces_vector():
    payload = schema.build_attest(
        node_id=NODE, season=5,
        season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z",
        hours_online=1, verified_hrs=1, reading_count=3,
        block_height_at_write=8794728, season_root_hex=VEC["season_root"],
        signed_at="2026-06-01T00:05:00Z",
    )
    signed = schema.sign_attest(payload, ORACLE_SEED)
    assert signed == VEC["records"]["attest"]


def test_attest_backcompat_data_hash_equals_season_root():
    a = VEC["records"]["attest"]
    assert a["data_hash"] == a["season_root"]


def test_verify_attest_accepts_and_detects_tamper():
    a = VEC["records"]["attest"]
    assert schema.verify_attest(a, ORACLE_PUB) is True
    tampered = {**a, "hours_online": 24}
    assert schema.verify_attest(tampered, ORACLE_PUB) is False


@pytest.mark.parametrize("name", ["meta", "node", "readings", "attest", "latest"])
def test_value_hex_round_trips(name):
    rec = VEC["records"][name]
    assert schema.parse_value(schema.value_hex(rec)) == rec


def test_parse_value_handles_garbage():
    assert schema.parse_value(None) is None
    assert schema.parse_value("") is None
    assert schema.parse_value("not-hex") is None
