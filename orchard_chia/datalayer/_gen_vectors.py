# SPDX-License-Identifier: Apache-2.0
"""Generate the golden cross-language test vectors for the publish schema.

Deterministic: fixed secp256r1 private scalars + fixed reading bodies, and
signing is RFC 6979 deterministic ECDSA (ADR-0007) with low-S normalization,
so re-running produces byte-identical output — and the firmware's mbedTLS
deterministic ECDSA produces the *same bytes* for the same inputs. The result,
``testdata/vectors.json``, is the contract the firmware (C++) and the public
verifier (JS/Python) must reproduce.

Run from the repo root:

    python -m orchard_chia.datalayer._gen_vectors
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import merkle, schema

OUT = Path(__file__).parent / "testdata" / "vectors.json"

# Fixed seeds — NOT secrets, only for reproducible fixtures.
DEVICE_SEED = bytes(range(1, 33)).hex()    # 01..20
ORACLE_SEED = bytes(range(33, 65)).hex()   # 21..40

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEASON = 5
HOUR = 13


def _reading_bodies() -> list[dict]:
    """Three unsigned readings (exercises promote-last at 3 leaves).

    Signed core is INTEGER fixed-point only (SPEC §0). gas_mv = round(adc *
    3300 / 4095). No floats anywhere under a signature.
    """
    return [
        {
            "node_id": NODE, "ts_ms": 1749480000123, "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {
                "temperature_mc": 21400, "humidity_milli_pct": 48200, "pressure_pa": 101260,
                "gas_adc_raw": 1234, "gas_mv": 994, "gps_fix": True, "gps_sats": 7,
            },
        },
        {
            "node_id": NODE, "ts_ms": 1749480060456, "block_anchor": "a1b2c3d4e5f60718",
            "metrics": {
                "temperature_mc": 21500, "humidity_milli_pct": 48000, "pressure_pa": 101270,
                "gas_adc_raw": 1240, "gas_mv": 999, "gps_fix": True, "gps_sats": 8,
            },
        },
        {
            "node_id": NODE, "ts_ms": 1749480120789, "block_anchor": "b2c3d4e5f6071829",
            "metrics": {
                "temperature_mc": 21600, "humidity_milli_pct": 47800, "pressure_pa": 101250,
                "gas_adc_raw": 1229, "gas_mv": 990, "gps_fix": True, "gps_sats": 8,
            },
        },
    ]


def _merkle_cases() -> list[dict]:
    """Known-answer Merkle roots for 1..5 leaves (covers promote-last)."""
    cases = []
    for n in range(1, 6):
        leaves = [hashlib.sha256(f"L{i}".encode()).digest() for i in range(n)]
        cases.append({
            "leaves": [x.hex() for x in leaves],
            "root": merkle.merkle_root(leaves).hex(),
        })
    return cases


def build() -> dict:
    device_pub = schema.pubkey_for_seed(DEVICE_SEED)
    oracle_pub = schema.pubkey_for_seed(ORACLE_SEED)

    signed = [schema.sign_reading(b, DEVICE_SEED) for b in _reading_bodies()]
    batch = schema.build_readings_batch(node_id=NODE, season=SEASON, hour=HOUR, readings=signed)
    hr = batch["hour_root"]
    sr = schema.season_root({HOUR: hr})

    readings_by_hour = {HOUR: signed}
    vhrs = schema.verified_hours(readings_by_hour, device_pub)

    attest = schema.build_attest(
        node_id=NODE, season=SEASON,
        season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z",
        hours_online=1, verified_hrs=vhrs, reading_count=len(signed),
        block_height_at_write=8794728, season_root_hex=sr,
        signed_at="2026-06-01T00:05:00Z",
    )
    attest_signed = schema.sign_attest(attest, ORACLE_SEED)

    node_rec = schema.build_node(
        node_id=NODE, pubkey=device_pub, board="freenove-esp32s3-uart", fw="0.4.7",
        sensors=[
            {"name": "mq135", "unit": "adc", "active": True},
            {"name": "bme280", "bus": "i2c", "active": True},
            {"name": "gps", "bus": "uart", "active": True},
        ],
        geohash="dr5ru", first_seen_utc="2026-05-28T20:43:27Z",
    )
    meta_rec = schema.build_meta(
        writer_version="0.1.0", created_at="2026-06-09T00:00:00Z", season_pubkey=oracle_pub
    )
    latest_rec = schema.build_latest(
        node_id=NODE, season=SEASON, hour=HOUR, last_sealed_season=4,
        running_hours_online=1, last_reading=signed[-1], updated_at="2026-05-31T13:02:01Z",
    )

    return {
        "schema_version": schema.SCHEMA_VERSION,
        "canonical_examples": [
            {"obj": {"b": 1, "a": 2}, "canonical": '{"a":2,"b":1}'},
            {"obj": {"z": [3, 2, 1], "a": {"y": 1, "x": 2}},
             "canonical": '{"a":{"x":2,"y":1},"z":[3,2,1]}'},
        ],
        "merkle": {"empty_root": merkle.EMPTY_ROOT.hex(), "cases": _merkle_cases()},
        "device": {"seed": DEVICE_SEED, "pubkey": device_pub},
        "oracle": {"seed": ORACLE_SEED, "pubkey": oracle_pub},
        "readings_signed": signed,
        # The EXACT bytes each device signs (canonical reading minus `sig`).
        # The firmware's hand-built canonical serializer must reproduce these
        # strings byte-for-byte, or its secp256r1 signatures won't verify.
        "reading_canonical": [
            schema.canonical_bytes({k: v for k, v in r.items() if k != "sig"}).decode("utf-8")
            for r in signed
        ],
        "reading_leaves": [schema.reading_leaf(r).hex() for r in schema._sorted_readings(signed)],
        "hour_root": hr,
        "season_root": sr,
        "verified_hours": vhrs,
        "season_score": schema.season_score(vhrs),
        "records": {"meta": meta_rec, "node": node_rec, "readings": batch,
                    "attest": attest_signed, "latest": latest_rec},
        "keys": {
            "meta": schema.meta_key(),
            "node": schema.node_key(NODE),
            "readings": schema.readings_key(NODE, SEASON, HOUR),
            "attest": schema.attest_key(NODE, SEASON),
            "latest": schema.latest_key(NODE),
        },
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
