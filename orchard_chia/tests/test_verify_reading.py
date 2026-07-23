# SPDX-License-Identifier: Apache-2.0
"""Per-reading verification helper (SPEC §8 Atlas 'Verify' primitive)."""
from __future__ import annotations

from orchard_chia.datalayer import schema, verify

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)
OTHER_PUB = schema.pubkey_for_seed("02" + "00" * 31)


def _reading(ts: int, temp: int = 21000) -> dict:
    return schema.sign_reading(
        {
            "node_id": NODE,
            "ts_ms": ts,
            "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {"temperature_mc": temp},
        },
        SEED,
    )


def _hour(readings):
    return schema.build_readings_batch(
        node_id=NODE, season=5, hour=13, readings=readings
    )


def test_valid_reading_verifies():
    r0, r1 = _reading(1000), _reading(2000, 22000)
    rec = _hour([r0, r1])
    res = verify.verify_reading_in_hour(r0, PUB, rec)
    assert res.ok is True
    assert res.signature_ok and res.in_hour_tree and res.hour_root_ok


def test_wrong_pubkey_fails_signature():
    r0 = _reading(1000)
    rec = _hour([r0])
    res = verify.verify_reading_in_hour(r0, OTHER_PUB, rec)
    assert res.signature_ok is False
    assert res.ok is False


def test_reading_not_in_hour_fails_membership():
    r0 = _reading(1000)
    foreign = _reading(9999)
    rec = _hour([r0])  # tree contains only r0
    res = verify.verify_reading_in_hour(foreign, PUB, rec)
    assert res.in_hour_tree is False
    assert res.ok is False


def test_tampered_hour_root_fails():
    r0 = _reading(1000)
    rec = _hour([r0])
    rec = {**rec, "hour_root": "00" * 32}
    res = verify.verify_reading_in_hour(r0, PUB, rec)
    assert res.hour_root_ok is False
    assert res.ok is False


def test_bad_data_does_not_raise():
    assert verify.verify_reading_in_hour(None, PUB, {}).ok is False
    assert verify.verify_reading_in_hour({}, None, None).ok is False
