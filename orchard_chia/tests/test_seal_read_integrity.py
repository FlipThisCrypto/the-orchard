# SPDX-License-Identifier: Apache-2.0
"""A DataLayer read failure must never be mistaken for 'nothing published'.

Regression for a confirmed integrity defect: load_season_readings soft-failed to
[] on any RPC error and silently skipped unreadable hours, so one transient blip
during the nightly attest permanently signed either a placeholder ("nothing
exists") or a season_root over a PARTIAL set with an understated
verified_hours — signed, labelled proof-backed, and irreversible.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import schema, seal
from orchard_chia.datalayer.rpc import ChiaRpcError

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def _batch(hour: int):
    r = schema.sign_reading(
        {"node_id": NODE, "ts_ms": 1000 + hour, "block_anchor": "a1b2c3d4e5f60718",
         "metrics": {"temperature_mc": 21000}},
        SEED,
    )
    return schema.build_readings_batch(node_id=NODE, season=5, hour=hour, readings=[r])


class _Rpc:
    """Serves two published hours; can be told to fail specific operations."""

    def __init__(self, *, keys_fail=False, fail_hour=None):
        self.keys_fail = keys_fail
        self.fail_hour = fail_hour
        self.store = {
            schema.readings_key(NODE, 5, h): schema.value_hex(_batch(h))
            for h in (1, 2)
        }

    def get_keys(self, store_id):          # soft variant
        return [] if self.keys_fail else list(self.store)

    def get_keys_strict(self, store_id):   # strict variant
        if self.keys_fail:
            raise ChiaRpcError("datalayer get_keys unreachable")
        return list(self.store)

    def _maybe_fail(self, key_hex):
        if self.fail_hour is not None:
            if key_hex == schema.readings_key(NODE, 5, self.fail_hour):
                raise ChiaRpcError("transient read failure")

    def get_value(self, store_id, key_hex):
        try:
            self._maybe_fail(key_hex)
        except ChiaRpcError:
            return None                     # the old swallowing behavior
        return self.store.get(key_hex)

    def get_value_strict(self, store_id, key_hex):
        self._maybe_fail(key_hex)
        return self.store[key_hex]


def test_unreachable_store_raises_instead_of_looking_empty():
    with pytest.raises(seal.SealReadError):
        seal.load_season_readings(
            _Rpc(keys_fail=True), "s", node_id=NODE, season=5, strict=True
        )


def test_unreadable_hour_raises_instead_of_being_dropped():
    with pytest.raises(seal.SealReadError):
        seal.load_season_readings(
            _Rpc(fail_hour=2), "s", node_id=NODE, season=5, strict=True
        )


def test_partial_read_would_understate_verified_hours_without_strict():
    """Demonstrates the damage the strict mode prevents."""
    lax = seal.load_season_readings(
        _Rpc(fail_hour=2), "s", node_id=NODE, season=5, strict=False
    )
    assert len(lax) == 1, "one hour silently dropped"
    sealed = seal.seal_from_readings(lax, device_pubkey=PUB)
    assert sealed.verified_hours == 1  # would be signed as the truth

    full = seal.load_season_readings(_Rpc(), "s", node_id=NODE, season=5, strict=True)
    assert seal.seal_from_readings(full, device_pubkey=PUB).verified_hours == 2


def test_healthy_store_reads_completely_in_strict_mode():
    rows = seal.load_season_readings(_Rpc(), "s", node_id=NODE, season=5, strict=True)
    assert len(rows) == 2
    assert seal.seal_from_readings(rows, device_pubkey=PUB).sigs_verified is True


def test_reconcile_style_lax_read_still_soft_fails():
    """Read-only reporting keeps its forgiving behavior."""
    assert seal.load_season_readings(
        _Rpc(keys_fail=True), "s", node_id=NODE, season=5
    ) == []
