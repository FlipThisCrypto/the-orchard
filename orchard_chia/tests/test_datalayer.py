# SPDX-License-Identifier: Apache-2.0
"""Tests for the pure functions in orchard_chia.datalayer.attest.

Hermetic — no oracle, no Chia node, no network. We just round-trip the
build/sign/serialize/parse logic and confirm the DataLayer key/value
encoding is deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchard_chia.datalayer import attest


NODE = "5B9BB022649FA93D4091DA4BA40714B9"
KEY  = "00112233445566778899AABBCCDDEEFF" "00112233445566778899AABBCCDDEEFF"  # 64 hex chars


def _payload():
    return attest.build_attestation_payload(
        node_id=NODE,
        season=42,
        hours_online=23,
        season_start_utc="2026-07-07T00:00:00+00:00",
        season_end_utc="2026-07-08T00:00:00+00:00",
        block_height_at_write=8392104,
        data_hash="a" * 64,
        signed_at=datetime(2026, 7, 8, 0, 5, 0, tzinfo=timezone.utc),
    )


def test_payload_shape():
    p = _payload()
    assert p["node_id"] == NODE
    assert p["season"] == 42
    assert p["hours_online"] == 23
    assert p["block_height_at_write"] == 8392104
    assert p["data_hash"] == "a" * 64
    assert p["signed_at"].startswith("2026-07-08T00:05:00")
    assert "oracle_sig" not in p  # signing adds this


def test_sign_verify_round_trip():
    p = _payload()
    signed = attest.sign_payload(p, KEY)
    assert "oracle_sig" in signed
    assert len(signed["oracle_sig"]) == 64  # HMAC-SHA256 hex
    assert attest.verify_signature(signed, KEY) is True


def test_sign_detects_tamper():
    p = _payload()
    signed = attest.sign_payload(p, KEY)
    # Flip a single field — verify must fail.
    tampered = dict(signed)
    tampered["hours_online"] = 24
    assert attest.verify_signature(tampered, KEY) is False


def test_sign_rejects_wrong_key():
    p = _payload()
    signed = attest.sign_payload(p, KEY)
    other_key = "FF" * 32
    assert attest.verify_signature(signed, other_key) is False


def test_datalayer_key_for_is_stable():
    k = attest.datalayer_key_for(NODE, 42)
    # Same inputs → same hex every time.
    assert attest.datalayer_key_for(NODE, 42) == k
    # Decoded back, it should be exactly "attest:<NODE>:00000042".
    decoded = bytes.fromhex(k).decode("utf-8")
    assert decoded == f"attest:{NODE}:00000042"


def test_datalayer_value_round_trip():
    p = _payload()
    signed = attest.sign_payload(p, KEY)
    v_hex = attest.datalayer_value_for(signed)
    parsed = attest.parse_datalayer_value(v_hex)
    assert parsed == signed


def test_data_hash_is_deterministic():
    h1 = attest.data_hash_for_uptime(NODE, 5, 23)
    h2 = attest.data_hash_for_uptime(NODE, 5, 23)
    assert h1 == h2
    # Different inputs → different hash.
    assert attest.data_hash_for_uptime(NODE, 5, 22) != h1


def test_node_id_normalized_to_upper():
    p_lower = attest.build_attestation_payload(
        node_id=NODE.lower(),
        season=1,
        hours_online=24,
        season_start_utc="x",
        season_end_utc="y",
        block_height_at_write=0,
        data_hash="z",
    )
    assert p_lower["node_id"] == NODE.upper()


def test_parse_datalayer_value_handles_garbage():
    assert attest.parse_datalayer_value(None) is None
    assert attest.parse_datalayer_value("") is None
    assert attest.parse_datalayer_value("not-hex") is None
    # Hex but not valid UTF-8 / JSON → returns None.
    assert attest.parse_datalayer_value("00" * 8) is None


def test_sign_and_verify_reject_malformed_key():
    """M4/L3: a signing key must be 64 hex chars — refuse to HMAC under a
    short/empty/non-hex key (which would be guessable/forgeable)."""
    p = _payload()
    for bad in ("", "tooshort", "zz" * 32, "a" * 63):
        with pytest.raises(ValueError):
            attest.sign_payload(p, bad)
    signed = attest.sign_payload(p, KEY)
    with pytest.raises(ValueError):
        attest.verify_signature(signed, "")
