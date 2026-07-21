# SPDX-License-Identifier: Apache-2.0
from orchard_chia.datalayer import reconcile, schema, seal


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
        node_id=NODE, season=2, hour=0, readings=[r]
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
        node_id=NODE, season=2, hour=3, readings=[r]
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
