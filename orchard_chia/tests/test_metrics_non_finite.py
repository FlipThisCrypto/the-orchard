# SPDX-License-Identifier: Apache-2.0
"""Malformed numeric sensor values must not abort DataLayer publishing."""
from __future__ import annotations

import math

from orchard_chia.datalayer.metrics import metrics_from_sensors


def test_non_finite_sensor_values_are_omitted_without_losing_valid_metrics():
    sensors = {
        "bme280": {
            "temperature_c": math.nan,
            "humidity_pct": math.inf,
            "pressure_hpa": "nan",
        },
        "ds18b20": {"temperature_c": "-inf"},
        "mq135": {"adc_raw": math.nan, "mv": math.inf},
        "pm": {"pm25_ugm3": math.inf, "pm10_ugm3": 12.5},
        "gps": {"satellites": math.nan, "fix": True},
    }

    assert metrics_from_sensors(sensors) == {
        "pm10_ugm3_x100": 1250,
        "gps_fix": True,
    }


def test_unrepresentably_large_integer_is_omitted():
    assert metrics_from_sensors({
        "bme280": {"temperature_c": 10**10000},
    }) == {}


def test_finite_values_that_overflow_fixed_point_scaling_are_omitted():
    assert metrics_from_sensors({
        "bme280": {"temperature_c": 1e308},
        "mq135": {"adc_raw": 1e308},
        "pm": {"pm25_ugm3": 1e308},
    }) == {}
