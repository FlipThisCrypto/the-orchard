# SPDX-License-Identifier: Apache-2.0
"""Tests for the DataLayer hot-path publisher (ADR-0003 / SPEC §6)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchard_chia.datalayer import metrics, publish, publish_watermark, schema


NODE = "5B9BB022649FA93D4091DA4BA40714B9"
# Non-zero scalar required for secp256r1 (ADR-0007); all-zero is invalid.
SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)


def _signed_reading(ts_ms: int = 1_749_480_000_123, temp_mc: int = 21400) -> dict:
    body = metrics.unsigned_reading_body(
        node_id=NODE,
        ts_ms=ts_ms,
        metrics={"temperature_mc": temp_mc, "gps_fix": True, "gps_sats": 7},
        block_anchor="a1b2c3d4e5f60718",
    )
    return schema.sign_reading(body, SEED)


def test_metrics_from_bme_and_mq():
    sensors = {
        "bme280": {"temperature_c": 21.4, "humidity_pct": 48.2, "pressure_hpa": 1012.60},
        "mq135": {"adc_raw": 1234.4},  # firmware field name (averaged float)
        "gps": {"fix": True, "sats": 7, "lat": 40.1, "lon": -74.2},
    }
    m = metrics.metrics_from_sensors(sensors)
    assert m["temperature_mc"] == 21400
    assert m["humidity_milli_pct"] == 48200
    assert m["pressure_pa"] == 101260
    assert m["gas_adc_raw"] == 1234
    assert m["gas_mv"] == round(1234 * 3300 / 4095)
    assert m["gps_fix"] is True
    assert m["gps_sats"] == 7
    assert "lat" not in m  # precise GPS never enters signed metrics


def test_extract_device_reading_nested_and_top_level():
    r = _signed_reading()
    assert metrics.extract_device_reading({"device_reading": r})["sig"] == r["sig"]
    assert metrics.extract_device_reading(r)["sig"] == r["sig"]
    assert metrics.extract_device_reading({"sensors": {"mq135": {"adc": 1}}}) is None


def test_plan_publish_writes_meta_readings_latest(tmp_path: Path):
    r1 = _signed_reading(ts_ms=1000)
    r2 = _signed_reading(ts_ms=2000, temp_mc=22000)
    batch = publish.HourBatchInput(
        node_id=NODE,
        season=5,
        hour=13,
        readings=[r1, r2],
        node_pubkey=PUB,
        board="freenove-esp32s3-uart",
        fw="0.4.7",
        sensors=[{"name": "bme280", "active": True}],
        geohash="dr5ru",
        first_seen_utc="2026-05-28T20:43:27Z",
        running_hours_online=14,
        last_sealed_season=4,
    )
    plan = publish.plan_publish(
        batches=[batch],
        season_pubkey=PUB,
        created_at="2026-06-09T13:00:30Z",
    )
    assert plan.meta_written
    assert NODE in plan.nodes_written
    assert len(plan.hours) == 1
    assert plan.hours[0][:3] == (NODE, 5, 13)

    # Decode keys written
    keys = []
    for item in plan.changelist:
        if item["action"] == "insert":
            keys.append(bytes.fromhex(item["key"]).decode())
    assert "meta:schema" in keys
    assert f"node:{NODE}" in keys
    assert f"readings:{NODE}:00000005:13" in keys
    assert f"latest:{NODE}" in keys

    # readings value is a valid batch with matching hour_root
    rk = schema.readings_key(NODE, 5, 13)
    val_hex = next(i["value"] for i in plan.changelist if i.get("key") == rk)
    rec = schema.parse_value(val_hex)
    assert rec["count"] == 2
    assert rec["hour_root"] == schema.hour_root([r1, r2])
    assert all(schema.verify_reading(x, PUB) for x in rec["readings"])


def test_plan_skips_unsigned_and_already_published():
    unsigned = metrics.unsigned_reading_body(
        node_id=NODE, ts_ms=1, metrics={"temperature_mc": 1}
    )
    plan = publish.plan_publish(
        batches=[
            publish.HourBatchInput(
                node_id=NODE, season=1, hour=0, readings=[unsigned]
            )
        ],
        season_pubkey=PUB,
    )
    assert plan.hours == []
    assert any("no device-signed" in s for s in plan.skipped)

    signed = _signed_reading()
    plan2 = publish.plan_publish(
        batches=[
            publish.HourBatchInput(
                node_id=NODE, season=1, hour=3, readings=[signed]
            )
        ],
        season_pubkey=PUB,
        already_published=lambda n, s, h: True,
    )
    assert plan2.hours == []
    assert any("already published" in s for s in plan2.skipped)


def test_plan_append_only_skips_existing_readings_key():
    signed = _signed_reading()
    rk = schema.readings_key(NODE, 2, 7)
    plan = publish.plan_publish(
        batches=[
            publish.HourBatchInput(
                node_id=NODE, season=2, hour=7, readings=[signed]
            )
        ],
        season_pubkey=PUB,
        existing_values={rk: "deadbeef"},
    )
    assert plan.hours == []
    assert any("append-only" in s for s in plan.skipped)


def test_plan_skips_readings_without_pubkey():
    # Signed readings but no pubkey in the batch and no node: card on chain →
    # the readings would be unverifiable, so they must not be published.
    signed = _signed_reading()
    plan = publish.plan_publish(
        batches=[
            publish.HourBatchInput(
                node_id=NODE, season=6, hour=1, readings=[signed], node_pubkey=None
            )
        ],
        season_pubkey=PUB,
    )
    assert plan.hours == []
    assert any("no device pubkey" in s for s in plan.skipped)


def test_plan_publishes_pubkeyless_batch_when_node_card_exists():
    # No pubkey in the batch, but a node: card is already on chain → the pubkey
    # is available to verifiers, so readings may be published.
    signed = _signed_reading()
    plan = publish.plan_publish(
        batches=[
            publish.HourBatchInput(
                node_id=NODE, season=6, hour=2, readings=[signed], node_pubkey=None
            )
        ],
        season_pubkey=PUB,
        existing_values={schema.node_key(NODE): "abcd"},  # card present
    )
    assert len(plan.hours) == 1


def test_plan_idempotent_meta_when_unchanged():
    meta = schema.build_meta(
        writer_version=publish.WRITER_VERSION,
        created_at="2026-06-09T13:00:30Z",
        season_pubkey=PUB,
    )
    mk = schema.meta_key()
    mv = schema.value_hex(meta)
    plan = publish.plan_publish(
        batches=[],
        season_pubkey=PUB,
        created_at="2026-06-09T13:00:30Z",
        existing_values={mk: mv},
    )
    assert plan.meta_written is False
    assert plan.changelist == []


def test_publish_watermark_roundtrip(tmp_path: Path):
    db = tmp_path / "wm.db"
    with publish_watermark.PublishWatermark(db) as wm:
        assert not wm.is_published(NODE, 5, 13)
        assert wm.count() == 0
        wm.record(node_id=NODE, season=5, hour=13, hour_root="abc", tx_id="tx1")
        assert wm.is_published(NODE, 5, 13)
        assert wm.count() == 1
        assert wm.last_published_hour(NODE, 5) == 13
        wm.record(node_id=NODE, season=5, hour=14, hour_root="def")
        assert wm.last_published_hour(NODE, 5) == 14
        assert wm.count() == 2
        # idempotent
        wm.record(node_id=NODE, season=5, hour=13, hour_root="zzz")
        assert wm.is_published(NODE, 5, 13)
        assert wm.count() == 2


def test_readings_from_oracle_payloads():
    r = _signed_reading()
    rows = [
        {"payload": {"sensors": {"mq135": {"adc": 1}}}},  # ignored
        {"payload": {"device_reading": r}},
        r,  # top-level also accepted
    ]
    got = publish.readings_from_oracle_payloads(rows, node_id=NODE)
    assert len(got) == 2
    assert all(schema.verify_reading(x, PUB) for x in got)


def test_hour_of_ts_ms_utc():
    # 2026-06-01T13:00:00Z
    ts = int(datetime_to_ms(2026, 6, 1, 13, 0, 0))
    assert publish.hour_of_ts_ms(ts) == 13


def datetime_to_ms(y, m, d, hh, mm, ss) -> int:
    from datetime import datetime, timezone
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp() * 1000)


def test_verify_bundle_over_planned_batch():
    """End-to-end: plan → decode → verify_bundle (offline)."""
    from orchard_chia.datalayer import verify

    r = _signed_reading()
    batch = publish.HourBatchInput(
        node_id=NODE,
        season=5,
        hour=13,
        readings=[r],
        node_pubkey=PUB,
        first_seen_utc="2026-05-28T20:43:27Z",
    )
    plan = publish.plan_publish(
        batches=[batch],
        season_pubkey=PUB,
        created_at="2026-06-09T13:00:30Z",
    )
    by_key = {
        bytes.fromhex(i["key"]).decode(): schema.parse_value(i["value"])
        for i in plan.changelist
        if i["action"] == "insert"
    }
    meta = by_key["meta:schema"]
    node = by_key[f"node:{NODE}"]
    readings_rec = by_key[f"readings:{NODE}:00000005:13"]
    # Sealed attest not produced by hot path — build a matching one for check 5–7.
    hour_roots = {13: readings_rec["hour_root"]}
    sr = schema.season_root(hour_roots)
    attest_body = schema.build_attest(
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
    )
    attest = schema.sign_attest(attest_body, SEED)
    report = verify.verify_bundle(
        meta=meta,
        node=node,
        attest=attest,
        readings_records=[readings_rec],
    )
    assert report.valid, [(c.name, c.detail) for c in report.checks if not c.ok]
