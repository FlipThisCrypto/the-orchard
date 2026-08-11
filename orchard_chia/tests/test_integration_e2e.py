# SPDX-License-Identifier: Apache-2.0
"""Capstone: raw sensors -> signed reading -> publish plan -> verify.

Walks the whole DataLayer pipeline end to end so a break anywhere between sensor
conversion and a VALID/Verified verdict is caught by one test.
"""
from __future__ import annotations

from orchard_chia.datalayer import metrics, publish, schema, verify

NODE = "5B9BB022649FA93D4091DA4BA40714B9"
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def test_sensors_to_verified_roundtrip():
    # 1. Raw firmware sensor payload -> integer fixed-point SPEC metrics.
    raw = {
        "bme280": {"temperature_c": 21.4, "humidity_pct": 48.2, "pressure_hpa": 1012.6},
        "mq135": {"adc_raw": 1234},
        "gps": {"fix": True, "sats": 7},
    }
    m = metrics.metrics_from_sensors(raw)
    assert m["temperature_mc"] == 21400 and m["gps_fix"] is True

    # 2. Sign the reading (no floats under the signature).
    body = metrics.unsigned_reading_body(
        node_id=NODE, ts_ms=1_780_232_400_123, metrics=m,
        block_anchor="a1b2c3d4e5f60718",
    )
    reading = schema.sign_reading(body, SEED)

    # A real hour is ~60 readings (firmware samples every 60 s), and an hour
    # only counts once it holds MIN_VERIFIED_READINGS_PER_HOUR signature-valid
    # ones. Publishing a single reading and calling it an hour is the exact
    # overstatement that rule exists to stop, so this capstone builds an hour
    # that could actually have happened.
    hour_readings = [reading] + [
        schema.sign_reading(
            metrics.unsigned_reading_body(
                node_id=NODE, ts_ms=1_780_232_400_123 + (i * 60_000), metrics=m,
                block_anchor="a1b2c3d4e5f60718",
            ), SEED)
        for i in range(1, schema.MIN_VERIFIED_READINGS_PER_HOUR)
    ]
    assert len(hour_readings) == schema.MIN_VERIFIED_READINGS_PER_HOUR

    # 3. Plan the hot-path publish and decode the changelist.
    batch = publish.HourBatchInput(
        node_id=NODE, season=5, hour=13, readings=hour_readings,
        node_pubkey=PUB, first_seen_utc="2026-05-28T20:43:27Z",
    )
    plan = publish.plan_publish(
        batches=[batch], season_pubkey=PUB, created_at="2026-06-09T13:00:30Z"
    )
    by_key = {
        bytes.fromhex(i["key"]).decode(): schema.parse_value(i["value"])
        for i in plan.changelist if i["action"] == "insert"
    }
    meta = by_key["meta:schema"]
    node = by_key[f"node:{NODE}"]
    readings_rec = by_key[f"readings:{NODE}:00000005:13"]

    # 4. Seal a matching attest and verify the whole bundle.
    sr = schema.season_root({13: readings_rec["hour_root"]})
    attest = schema.sign_attest(
        schema.build_attest(
            node_id=NODE, season=5,
            season_start_utc="2026-05-31T00:00:00Z",
            season_end_utc="2026-06-01T00:00:00Z",
            hours_online=1, verified_hrs=1, reading_count=len(hour_readings),
            block_height_at_write=1, season_root_hex=sr,
            signed_at="2026-06-01T00:05:00Z",
        ),
        SEED,
    )
    rep = verify.verify_bundle(
        meta=meta, node=node, attest=attest, readings_records=[readings_rec]
    )
    assert rep.valid, [(c.name, c.detail) for c in rep.checks if not c.ok]
    assert verify.verification_badge(rep, sealed=True) == "Verified"

    # 5. Per-reading verify (SPEC §8) on the same datum.
    rc = verify.verify_reading_in_hour(reading, PUB, readings_rec)
    assert rc.ok is True

    # 6. Payout pays on the verifiable verified_hours.
    from orchard_chia.payout import calculator
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == round(
        (1 / 24) * 1000
    )
