# SPDX-License-Identifier: Apache-2.0
"""End-to-end coverage of `orchard-verify live` (fetch -> verify -> inclusion)."""
from __future__ import annotations

import pytest

from orchard_chia.cli import orchard_verify as cli
from orchard_chia.datalayer import schema
from orchard_chia.datalayer.inclusion import clvm_hash
from orchard_chia.datalayer.config import (
    AttestationConfig, Config, DataLayerConfig, FullNodeConfig, OracleConfig,
)

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def _store() -> dict[str, str]:
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
            node_id=NODE, season=5,
            season_start_utc="2026-05-31T00:00:00Z",
            season_end_utc="2026-06-01T00:00:00Z",
            hours_online=1, verified_hrs=1, reading_count=1,
            block_height_at_write=1, season_root_hex=sr,
            signed_at="2026-06-01T00:05:00Z",
        ),
        SEED,
    )
    meta = schema.build_meta(
        writer_version="0.2.0", created_at="2026-06-09T00:00:00Z", season_pubkey=PUB
    )
    node = schema.build_node(
        node_id=NODE, pubkey=PUB, board="t", fw="0.4.8", sensors=[],
        geohash="dr5ru", first_seen_utc="2026-05-28T20:43:27Z",
    )
    return {
        schema.meta_key(): schema.value_hex(meta),
        schema.node_key(NODE): schema.value_hex(node),
        schema.attest_key(NODE, 5): schema.value_hex(attest),
        schema.readings_key(NODE, 5, 13): schema.value_hex(readings),
    }


class FakeRpc:
    """Serves reads + inclusion proofs consistent with a store dict.

    ``bad_value_keys`` simulates a tampered mirror: get_value returns the real
    value but the proof commits to a different value_clvm_hash for those keys.
    """
    def __init__(self, store, bad_value_keys=frozenset()):
        self.store = store
        self.bad_value_keys = set(bad_value_keys)

    def get_value(self, store_id, key_hex):
        return self.store.get(key_hex)

    def get_keys(self, store_id):
        return list(self.store.keys())

    def get_root(self, store_id):
        return {"success": True, "hash": "ab" * 32, "confirmed": True}

    def get_proof(self, store_id, keys):
        proofs = []
        for k in keys:
            v = self.store.get(k, "")
            vch = "11" * 32 if k in self.bad_value_keys else (
                clvm_hash(v) if v else "00" * 32
            )
            proofs.append({
                "key_clvm_hash": clvm_hash(k),
                "value_clvm_hash": vch,
                "node_hash": "ee" * 32,
                "layers": [],
            })
        return {
            "success": True,
            "proof": {
                "coin_id": "de" * 32,
                "inner_puzzle_hash": "ad" * 32,
                "store_proofs": {"store_id": store_id, "proofs": proofs},
            },
        }

    def verify_proof(self, coin_id, inner_puzzle_hash, store_proofs):
        return {"success": True, "current_root": True}


def _wire(monkeypatch, store, bad_value_keys=frozenset()):
    fake_cfg = Config(
        network="test",
        full_node=FullNodeConfig("127.0.0.1", 8555, "c", "k"),
        data_layer=DataLayerConfig("127.0.0.1", 8562, "c", "k", "cd" * 32),
        oracle=OracleConfig("http://x"),
        attestation=AttestationConfig(),
        signing_key_hex="ab" * 32,
    )
    monkeypatch.setattr(cli.config, "load", lambda: fake_cfg)
    monkeypatch.setattr(
        cli, "DataLayerRpc", lambda *a, **k: FakeRpc(store, bad_value_keys)
    )


def test_cmd_live_happy_path_valid(monkeypatch, capsys):
    _wire(monkeypatch, _store())
    rc = cli.main(["live", "--node-id", NODE, "--season", "5"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Result: VALID" in out


def test_cmd_live_tampered_mirror_value_bind_fails(monkeypatch, capsys):
    # Offline data reads valid, but the proof commits to a different value hash
    # for the readings key (tampered/stale mirror). Inclusion value-bind must
    # catch it — a definitive contradiction → exit 1 (INVALID), not 2.
    store = _store()
    _wire(monkeypatch, store, bad_value_keys={schema.readings_key(NODE, 5, 13)})
    rc = cli.main(["live", "--node-id", NODE, "--season", "5"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "INVALID" in out
