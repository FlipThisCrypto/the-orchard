# SPDX-License-Identifier: Apache-2.0
"""The sensor bonus is earned by instruments, not declarations.

Weight up to 1.25x keyed on sensor names from the LATEST payload meant six
junk keys in one reading farmed the maximum bonus forever. Qualifying now
requires an approved measurement class AND persistent reporting — and a class
counts once however many devices report it, because the spec pays for data
diversity, not for owning two thermometers.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from oracle.app import db, models
from oracle.app.sensor_classes import (classes_for, qualifying_sensor_classes)

NODE = "D8641AD6CAE36977818499469F7E8C49"
SEASON = 74


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL",
                       f"sqlite:///{(tmp_path/'s.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    s = db.session_factory()()
    s.add(models.Node(node_id=NODE, signing_key_hex="ab" * 32, last_seq=0,
                      registered_at=dt.datetime.now(dt.timezone.utc)))
    s.commit()
    yield s
    s.close()


def _reading(session, hour_idx: int, sensors: dict):
    from oracle.app import seasons
    bucket = seasons.hour_buckets_in_season(SEASON)[hour_idx]
    when = dt.datetime.strptime(bucket, "%Y-%m-%dT%H").replace(
        tzinfo=dt.timezone.utc)
    session.add(models.Reading(
        node_id=NODE, received_at=when,
        payload_json=json.dumps({"sensors": sensors}), sig_hex="ab" * 32))
    session.commit()


def test_a_sensor_reporting_all_day_qualifies(session):
    for h in range(14):
        _reading(session, h, {"ds18b20": {"temperature_c": 20.1}})
    n, classes = qualifying_sensor_classes(session, NODE, SEASON)
    assert n == 1 and classes == ["temperature"]


def test_a_one_off_declaration_does_not(session):
    _reading(session, 3, {"ds18b20": {"t": 20}, "sgp30": 1, "pms5003": 2,
                          "scd40": 3, "bh1750": 4, "soil": 5})
    n, classes = qualifying_sensor_classes(session, NODE, SEASON)
    assert n == 0 and classes == []


def test_unknown_sensor_names_earn_nothing(session):
    for h in range(20):
        _reading(session, h, {"totally_real_sensor_9000": 42})
    assert qualifying_sensor_classes(session, NODE, SEASON) == (0, [])


def test_two_thermometers_count_once(session):
    """Redundancy is not diversity."""
    for h in range(14):
        _reading(session, h, {"ds18b20": {"t": 20}, "sht31": {"t": 21}})
    n, classes = qualifying_sensor_classes(session, NODE, SEASON)
    assert "temperature" in classes
    assert n == len(classes), "each class exactly once"
    assert classes.count("temperature") == 1


def test_a_multi_measurement_device_earns_each_class(session):
    for h in range(14):
        _reading(session, h, {"bme280": {"t": 20, "h": 50, "p": 1012}})
    n, classes = qualifying_sensor_classes(session, NODE, SEASON)
    assert set(classes) == {"temperature", "humidity", "pressure"} and n == 3


def test_null_values_are_declarations_not_measurements(session):
    for h in range(20):
        _reading(session, h, {"ds18b20": None})
    assert qualifying_sensor_classes(session, NODE, SEASON) == (0, [])


def test_the_class_map_is_extensible_data():
    """Governance extends a dict, not logic."""
    from oracle.app.sensor_classes import APPROVED_SENSOR_CLASSES
    assert isinstance(APPROVED_SENSOR_CLASSES, dict)
    assert classes_for("DS18B20") == {"temperature"}, "case-insensitive"
    assert classes_for("unknown") == set()


def test_the_uptime_endpoint_carries_the_qualified_classes(session):
    """What the settlement runner reads."""
    from fastapi.testclient import TestClient
    from oracle.app.main import app
    for h in range(14):
        _reading(session, h, {"ds18b20": {"t": 20}})
    with TestClient(app) as c:
        j = c.get(f"/uptime/{NODE}/{SEASON}").json()
    assert j["qualifying_sensor_count"] == 1
    assert j["qualifying_sensor_classes"] == ["temperature"]


def test_impossible_values_do_not_qualify(session):
    """5000 degrees, persistently, is not a thermometer."""
    for h in range(20):
        _reading(session, h, {"ds18b20": {"temperature_c": 5000.0}})
    assert qualifying_sensor_classes(session, NODE, SEASON) == (0, [])


def test_a_disconnected_probe_reading_does_not_qualify(session):
    """-127 is the DS18B20's disconnected sentinel."""
    for h in range(20):
        _reading(session, h, {"ds18b20": {"temperature_c": -127.0}})
    assert qualifying_sensor_classes(session, NODE, SEASON) == (0, [])


def test_plausible_extremes_still_qualify(session):
    """Ranges are generous: record cold on Earth must never clip an honest
    operator."""
    for h in range(14):
        _reading(session, h, {"ds18b20": {"temperature_c": -67.8}})
    n, classes = qualifying_sensor_classes(session, NODE, SEASON)
    assert classes == ["temperature"]


def test_unknown_fields_are_not_judged(session):
    for h in range(14):
        _reading(session, h, {"ds18b20": {"temperature_c": 20.0,
                                          "vendor_diag": 99999}})
    assert qualifying_sensor_classes(session, NODE, SEASON)[0] == 1


def test_a_bad_spell_only_costs_those_hours(session):
    """12 good hours + 8 impossible ones: still qualifies on the good 12."""
    for h in range(12):
        _reading(session, h, {"ds18b20": {"temperature_c": 20.0}})
    for h in range(12, 20):
        _reading(session, h, {"ds18b20": {"temperature_c": 5000.0}})
    assert qualifying_sensor_classes(session, NODE, SEASON)[0] == 1
