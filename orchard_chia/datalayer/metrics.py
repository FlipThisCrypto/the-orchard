# SPDX-License-Identifier: Apache-2.0
"""Convert oracle/firmware sensor payloads into SPEC integer fixed-point metrics.

The DataLayer reading object (SPEC §2.3) forbids floats under a signature.
Trees today POST a nested ``sensors`` object with human floats; this module
maps that into the integer metric keys the schema and dashboard use.

Does **not** sign. The device (or a test harness with a known seed) must
produce ``sig`` over the canonical reading body.
"""
from __future__ import annotations

from typing import Any


def _as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _as_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v, 10)
        except ValueError:
            return None
    return None


def metrics_from_sensors(sensors: dict | None) -> dict[str, int | bool]:
    """Map a firmware ``sensors`` object to SPEC integer metrics.

    Only keys the sensors actually provide are included. Unknown / missing
    sensors are omitted (a Tree emits the subset it supports — SPEC §2.3).
    """
    if not isinstance(sensors, dict):
        return {}

    out: dict[str, int | bool] = {}

    # BME280 / similar — common field names from current firmware payloads.
    bme = sensors.get("bme280") if isinstance(sensors.get("bme280"), dict) else None
    if bme:
        t = _as_float(bme.get("temperature_c") if "temperature_c" in bme else bme.get("temp_c"))
        if t is not None:
            out["temperature_mc"] = int(round(t * 1000))
        h = _as_float(bme.get("humidity_pct") if "humidity_pct" in bme else bme.get("humidity"))
        if h is not None:
            out["humidity_milli_pct"] = int(round(h * 1000))
        p = _as_float(bme.get("pressure_hpa") if "pressure_hpa" in bme else bme.get("pressure"))
        if p is not None:
            # Prefer hPa → Pa; if value looks like Pa already (>2000), keep as Pa.
            out["pressure_pa"] = int(round(p * 100)) if p < 2000 else int(round(p))

    # DS18B20 temperature (only if BME didn't already set it).
    ds = sensors.get("ds18b20") if isinstance(sensors.get("ds18b20"), dict) else None
    if ds and "temperature_mc" not in out:
        t = _as_float(ds.get("temperature_c") if "temperature_c" in ds else ds.get("temp_c"))
        if t is not None:
            out["temperature_mc"] = int(round(t * 1000))

    # MQ-135 gas
    mq = sensors.get("mq135") if isinstance(sensors.get("mq135"), dict) else None
    if mq:
        adc = _as_int(mq.get("adc") if "adc" in mq else mq.get("raw"))
        if adc is not None:
            out["gas_adc_raw"] = adc
            # SPEC: gas_mv = round(gas_adc_raw * 3300 / 4095)
            out["gas_mv"] = int(round(adc * 3300 / 4095))
        mv = _as_int(mq.get("mv") if "mv" in mq else mq.get("gas_mv"))
        if mv is not None and "gas_mv" not in out:
            out["gas_mv"] = mv

    # Particulate (optional future sensors)
    pm = sensors.get("pm") if isinstance(sensors.get("pm"), dict) else None
    if pm:
        pm25 = _as_float(pm.get("pm25_ugm3") if "pm25_ugm3" in pm else pm.get("pm25"))
        if pm25 is not None:
            out["pm25_ugm3_x100"] = int(round(pm25 * 100))
        pm10 = _as_float(pm.get("pm10_ugm3") if "pm10_ugm3" in pm else pm.get("pm10"))
        if pm10 is not None:
            out["pm10_ugm3_x100"] = int(round(pm10 * 100))

    # GPS — fix + sats only (precise lat/lon stay off-chain by default).
    gps = sensors.get("gps") if isinstance(sensors.get("gps"), dict) else None
    if gps:
        if "fix" in gps:
            out["gps_fix"] = bool(gps["fix"])
        sats = _as_int(gps.get("sats") if "sats" in gps else gps.get("satellites"))
        if sats is not None:
            out["gps_sats"] = sats

    return out


def extract_device_reading(payload: dict | None) -> dict | None:
    """Return a SPEC reading object if the oracle payload already carries one.

    Supported shapes (first match wins):
      1. ``payload["device_reading"]`` — nested full reading (preferred).
      2. ``payload`` itself has ``metrics`` + ``sig`` + ``ts_ms`` — already SPEC.
    """
    if not isinstance(payload, dict):
        return None

    nested = payload.get("device_reading")
    if isinstance(nested, dict) and nested.get("sig") and nested.get("metrics") is not None:
        return dict(nested)

    if (
        payload.get("sig")
        and isinstance(payload.get("metrics"), dict)
        and payload.get("ts_ms") is not None
    ):
        # Strip non-SPEC top-level transport fields if present.
        keys = ("node_id", "ts_ms", "block_anchor", "metrics", "sig")
        return {k: payload[k] for k in keys if k in payload}

    return None


def unsigned_reading_body(
    *,
    node_id: str,
    ts_ms: int,
    metrics: dict[str, int | bool],
    block_anchor: str = "0000000000000000",
) -> dict:
    """SPEC reading body **without** ``sig`` — ready for device/test signing."""
    return {
        "node_id": node_id.upper(),
        "ts_ms": int(ts_ms),
        "block_anchor": block_anchor,
        "metrics": metrics,
    }
