# SPDX-License-Identifier: Apache-2.0
from orchard_chia.datalayer import reconcile, schema


def _full_hour(reading: dict) -> list[dict]:
    """An hour that meets the production signature quorum.

    Re-signs the same body at one-minute steps, which is what the firmware
    actually does (it samples every 60 s). Distinct ts_ms matters: identical
    readings would be duplicate Merkle leaves, and an hour padded with copies
    is not an hour of sensing either.
    """
    out = [reading]
    for i in range(1, schema.MIN_VERIFIED_READINGS_PER_HOUR):
        body = {k: v for k, v in reading.items() if k != "sig"}
        body["ts_ms"] = int(reading["ts_ms"]) + (i * 60_000)
        out.append(schema.sign_reading(body, SEED))
    return out


NODE = "AABBCCDDEEFF0011AABBCCDDEEFF0011"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def test_reconcile_overclaim():
    r = schema.sign_reading(
        {
            "node_id": NODE,
            "ts_ms": 1,
            "block_anchor": "a" * 16,
            "metrics": {"temperature_mc": 20000},
        },
        SEED,
    )
    batch = schema.build_readings_batch(
        node_id=NODE, season=2, hour=0, readings=_full_hour(r)
    )

    class FakeOracle:
        def get_uptime(self, node_id, season):
            return {"hours_online": 24}

    class FakeDl:
        def get_keys(self, store_id):
            return [schema.readings_key(NODE, 2, 0)]

        def get_value(self, store_id, key_hex):
            return schema.value_hex(batch)

    row = reconcile.reconcile_node_season(
        oracle=FakeOracle(),
        dl=FakeDl(),
        store_id="s",
        node_id=NODE,
        season=2,
        device_pubkey=PUB,
    )
    assert row.status == "overclaim"
    assert row.verified_hours == 1
    assert row.hours_online == 24


def test_reconcile_main_exits_datalayer_when_store_unreachable(monkeypatch):
    from orchard_chia.datalayer import exit_codes
    from orchard_chia.datalayer.config import (
        AttestationConfig, Config, DataLayerConfig, FullNodeConfig, OracleConfig,
    )
    from orchard_chia.datalayer.rpc import ChiaRpcError

    fake = Config(
        network="test",
        full_node=FullNodeConfig("127.0.0.1", 8555, "c", "k"),
        data_layer=DataLayerConfig("127.0.0.1", 8562, "c", "k", "ab" * 32),
        oracle=OracleConfig("http://x"),
        attestation=AttestationConfig(),
        signing_key_hex="ab" * 32,
    )
    monkeypatch.setattr(reconcile.config, "load", lambda: fake)

    class FakeOracle:
        def __init__(self, url, writer_token=None):
            pass

        def current_season(self):
            return 3

        def list_nodes(self):
            return [{"node_id": NODE}]

    monkeypatch.setattr(reconcile, "OracleClient", FakeOracle)

    class DownDl:
        def __init__(self, *a, **k):
            pass

        def get_root(self, store_id):
            raise ChiaRpcError("connection refused")

    monkeypatch.setattr(reconcile, "DataLayerRpc", DownDl)

    assert reconcile.main([]) == exit_codes.DATALAYER


def test_reconcile_match():
    r = schema.sign_reading(
        {
            "node_id": NODE,
            "ts_ms": 1,
            "block_anchor": "a" * 16,
            "metrics": {"temperature_mc": 20000},
        },
        SEED,
    )
    batch = schema.build_readings_batch(
        node_id=NODE, season=2, hour=3, readings=_full_hour(r)
    )

    class FakeOracle:
        def get_uptime(self, node_id, season):
            return {"hours_online": 1}

    class FakeDl:
        def get_keys(self, store_id):
            return [schema.readings_key(NODE, 2, 3)]

        def get_value(self, store_id, key_hex):
            return schema.value_hex(batch)

    row = reconcile.reconcile_node_season(
        oracle=FakeOracle(),
        dl=FakeDl(),
        store_id="s",
        node_id=NODE,
        season=2,
        device_pubkey=PUB,
    )
    assert row.status == "match"
