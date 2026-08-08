# SPDX-License-Identifier: Apache-2.0
"""Tests for live DataLayer bundle fetch (orchard-verify live path)."""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import fetch, schema


NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


class FakeRpc:
    def __init__(self, store: dict[str, str]):
        self.store = store

    def get_value(self, store_id: str, key_hex: str) -> str | None:
        return self.store.get(key_hex)

    def get_keys(self, store_id: str) -> list[str]:
        return list(self.store.keys())


def _bundle_store() -> dict[str, str]:
    reading = schema.sign_reading(
        {
            "node_id": NODE,
            "ts_ms": 1_749_480_000_123,
            "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {"temperature_mc": 21400, "gps_fix": True, "gps_sats": 7},
        },
        SEED,
    )
    readings = schema.build_readings_batch(
        node_id=NODE, season=5, hour=13, readings=[reading]
    )
    sr = schema.season_root({13: readings["hour_root"]})
    attest = schema.sign_attest(
        schema.build_attest(
            node_id=NODE,
            season=5,
            season_start_utc="2026-05-31T00:00:00Z",
            season_end_utc="2026-06-01T00:00:00Z",
            hours_online=1,
            verified_hrs=1,
            reading_count=1,
            block_height_at_write=1,
            season_root_hex=sr,
            signed_at="2026-06-01T00:05:00Z",
        ),
        SEED,
    )
    meta = schema.build_meta(
        writer_version="0.2.0",
        created_at="2026-06-09T00:00:00Z",
        season_pubkey=PUB,
    )
    node = schema.build_node(
        node_id=NODE,
        pubkey=PUB,
        board="test",
        fw="0.4.8",
        sensors=[],
        geohash="dr5ru",
        first_seen_utc="2026-05-28T20:43:27Z",
    )
    return {
        schema.meta_key(): schema.value_hex(meta),
        schema.node_key(NODE): schema.value_hex(node),
        schema.attest_key(NODE, 5): schema.value_hex(attest),
        schema.readings_key(NODE, 5, 13): schema.value_hex(readings),
    }


def test_fetch_bundle_happy_path():
    rpc = FakeRpc(_bundle_store())
    bundle = fetch.fetch_bundle(
        rpc, "store", node_id=NODE, season=5, hours=[13]
    )
    assert bundle["node"]["pubkey"] == PUB
    assert len(bundle["readings_records"]) == 1
    from orchard_chia.datalayer import verify
    rep = verify.verify_bundle(**bundle)
    assert rep.valid


def test_fetch_discovers_hours():
    rpc = FakeRpc(_bundle_store())
    bundle = fetch.fetch_bundle(rpc, "store", node_id=NODE, season=5, hours=None)
    assert [r["hour"] for r in bundle["readings_records"]] == [13]


def test_fetch_missing_meta():
    store = _bundle_store()
    del store[schema.meta_key()]
    with pytest.raises(fetch.FetchError, match="meta:schema"):
        fetch.fetch_bundle(FakeRpc(store), "s", node_id=NODE, season=5, hours=[13])
