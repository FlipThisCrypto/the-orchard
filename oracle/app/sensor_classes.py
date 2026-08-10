# SPDX-License-Identifier: Apache-2.0
"""Qualifying sensors — what actually earns the sensor bonus.

The reward model weights a Tree up to 1.25x for additional QUALIFYING sensors.
Until this module, "qualifying" meant "named in the latest reading's payload":
six junk keys in a single reading farmed the maximum bonus, permanently,
because nothing ever looked at a second reading. That is the exact attack the
tokenomics spec lists under anti-gaming ("fake sensor declarations",
"redundant sensors added solely to farm bonuses").

Two requirements now, both cheap to meet honestly and annoying to fake:

  1. APPROVED CLASS. A sensor name must map to an approved measurement class.
     Unknown names earn nothing (they still flow through the data pipeline —
     this gates the bonus, not the telemetry). Two sensors in the same class
     count ONCE: a second thermometer is redundancy, not diversity, and the
     spec is explicit that diversity is what the bonus buys.

  2. PERSISTENCE. The sensor must have reported in at least
     ``SENSOR_QUALIFY_MIN_HOURS`` distinct hours of the season. A key that
     appeared once at 3am is a declaration; a sensor that reports all day is
     an instrument.

The class map is deliberately a plain dict so governance can extend it without
touching logic, and so "approved sensor classes" can later gain per-class
value-range validation in one obvious place.
"""
from __future__ import annotations

import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, seasons

# sensor name (as firmware reports it) -> measurement class.
# One class counts once toward the bonus however many devices report it.
APPROVED_SENSOR_CLASSES: dict[str, str] = {
    # temperature
    "ds18b20": "temperature",
    "bme280": "temperature+humidity+pressure",   # multi-measurement device
    "dht22": "temperature+humidity",
    "sht31": "temperature+humidity",
    # air
    "mq135": "gas",
    "sgp30": "gas",
    "scd40": "co2",
    "pms5003": "particulates",
    "sds011": "particulates",
    # light / weather / soil
    "bh1750": "light",
    "veml6070": "uv",
    "rain": "rain",
    "soil": "soil",
    "capacitive_soil": "soil",
    # position (GPS earns data-class credit: it is telemetry, not just a fix)
    "gps": "position",
}

# A multi-measurement device (bme280) contributes each of its classes.
def classes_for(sensor_name: str) -> set[str]:
    cls = APPROVED_SENSOR_CLASSES.get(sensor_name.strip().lower())
    return set(cls.split("+")) if cls else set()


SENSOR_QUALIFY_MIN_HOURS = 12   # half a day: a sensor, not a declaration

# Physical plausibility per reported field. A value outside its range means
# the reading, whatever produced it, is not a measurement of the quantity the
# bonus pays for — a disconnected probe reads -127, a shorted ADC rails high,
# and a fabricated payload can claim anything. Out-of-range values do not
# count toward a sensor's persistence (the hour is ignored for that sensor);
# fields not listed here are not judged. Ranges are generous on purpose:
# the record low/high on Earth sit comfortably inside them, so an honest
# operator can never be clipped by weather.
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "temperature_c": (-90.0, 60.0),
    "humidity_pct": (0.0, 100.0),
    "pressure_hpa": (300.0, 1100.0),
    "co2_ppm": (250.0, 40000.0),
    "pm25_ugm3": (0.0, 1000.0),
    "pm10_ugm3": (0.0, 2000.0),
    "lux": (0.0, 200000.0),
    "uv_index": (0.0, 15.0),
    "soil_moisture_pct": (0.0, 100.0),
}


def _values_plausible(value: object) -> bool:
    """True unless a KNOWN field carries an impossible number."""
    if not isinstance(value, dict):
        return True          # scalar / bool payloads carry no judged fields
    for field, v in value.items():
        rng = PLAUSIBLE_RANGES.get(str(field).lower())
        if rng is None or not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if not (rng[0] <= float(v) <= rng[1]):
            return False
    return True


def qualifying_sensor_classes(db: Session, node_id: str, season: int,
                              *, min_hours: int = SENSOR_QUALIFY_MIN_HOURS
                              ) -> tuple[int, list[str]]:
    """(count, sorted classes) of measurement classes that qualified.

    Scans the season's stored readings once and counts, per sensor name, the
    distinct hours it appeared in. Classes are credited when ANY approved
    sensor of that class met the persistence bar — so two thermometers give
    the class two chances to qualify but never two credits.
    """
    buckets = set(seasons.hour_buckets_in_season(season))
    rows = db.execute(
        select(models.Reading.payload_json, models.Reading.received_at)
        .where(models.Reading.node_id == node_id.upper())
    ).all()

    hours_by_sensor: dict[str, set[str]] = {}
    for payload_json, received_at in rows:
        bucket = received_at.strftime("%Y-%m-%dT%H") if received_at else None
        if bucket not in buckets:
            continue
        try:
            sensors = (json.loads(payload_json) or {}).get("sensors") or {}
        except (ValueError, TypeError):
            continue
        if not isinstance(sensors, dict):
            continue
        for name, value in sensors.items():
            if value is None:
                continue        # a null is a declaration, not a measurement
            if not _values_plausible(value):
                continue        # an impossible number is not a measurement either
            hours_by_sensor.setdefault(str(name).lower(), set()).add(bucket)

    qualified: set[str] = set()
    for name, hours in hours_by_sensor.items():
        if len(hours) >= max(1, min_hours):
            qualified |= classes_for(name)
    return len(qualified), sorted(qualified)
