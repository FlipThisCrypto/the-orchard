# SPDX-License-Identifier: Apache-2.0
"""extract_device_reading must strip to the signed SPEC keys in BOTH shapes,
so a stray field can't ride along and invalidate the device signature."""
from __future__ import annotations

from orchard_chia.datalayer.metrics import extract_device_reading

SIGNED_KEYS = {"node_id", "ts_ms", "block_anchor", "metrics", "sig"}


def test_nested_shape_strips_extra_fields():
    payload = {
        "device_reading": {
            "node_id": "AA" * 16,
            "ts_ms": 1700000000000,
            "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {"temperature_mc": 21000},
            "sig": "ab" * 64,
            # transport noise that was NOT part of the signed bytes:
            "received_at": "2026-07-23T00:00:00Z",
            "voltage_v": 3.3,
        }
    }
    r = extract_device_reading(payload)
    assert r is not None
    assert set(r) == SIGNED_KEYS
    assert "received_at" not in r and "voltage_v" not in r


def test_flat_shape_strips_extra_fields():
    payload = {
        "node_id": "AA" * 16,
        "ts_ms": 1700000000000,
        "block_anchor": "a1b2c3d4e5f60718",
        "metrics": {"temperature_mc": 21000},
        "sig": "ab" * 64,
        "server_id": "x",
    }
    r = extract_device_reading(payload)
    assert r is not None
    assert set(r) == SIGNED_KEYS


def test_unsigned_payload_returns_none():
    assert extract_device_reading({"metrics": {"t": 1}, "ts_ms": 1}) is None
    assert extract_device_reading(None) is None
