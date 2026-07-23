# SPDX-License-Identifier: Apache-2.0
"""Every scalar metric the publisher can emit must have a unit in meta:schema.

The dashboard renders a value as raw * 10**pow10 using meta:schema.units
(from DEFAULT_UNITS). A metric key with no unit is unrenderable, so guard that
metrics_from_sensors never emits a scalar key absent from DEFAULT_UNITS.
"""
from __future__ import annotations

from orchard_chia.datalayer import metrics, schema

# bool metrics carry no scale (rendered as yes/no), so they need no unit.
_BOOL_METRICS = {"gps_fix"}


def test_all_emitted_scalar_metrics_have_units():
    rich_sensors = {
        "bme280": {"temperature_c": 21.4, "humidity_pct": 48.2, "pressure_hpa": 1012.6},
        "ds18b20": {"temperature_c": 20.0},
        "mq135": {"adc_raw": 1234, "mv": 990},
        "pm": {"pm25_ugm3": 12.3, "pm10_ugm3": 18.5},
        "gps": {"fix": True, "sats": 7},
    }
    emitted = set(metrics.metrics_from_sensors(rich_sensors))
    scalar = emitted - _BOOL_METRICS
    missing = scalar - set(schema.DEFAULT_UNITS)
    assert not missing, f"metric keys emitted without a unit in DEFAULT_UNITS: {missing}"


def test_units_have_pow10_and_display():
    # Each unit entry must carry the fields the dashboard scales/labels with.
    for key, spec in schema.DEFAULT_UNITS.items():
        assert "pow10" in spec, f"{key} unit missing pow10"
        assert "display" in spec, f"{key} unit missing display"
        assert isinstance(spec["pow10"], int)
