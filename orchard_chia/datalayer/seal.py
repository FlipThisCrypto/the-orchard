# SPDX-License-Identifier: Apache-2.0
"""Build a sealed Season attest from published ``readings:`` hour batches.

When the hot-path publisher has written device-signed hours to DataLayer,
the sealed ``attest:`` record must commit to those hours' Merkle roots
(SPEC §2.4 / §5) and recompute ``verified_hours`` from public data — not
from the oracle's private uptime claim alone.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import schema


@dataclass(frozen=True)
class SealInputs:
    """Material used to seal one node·season."""
    season_root: str
    verified_hours: int
    reading_count: int
    hour_count: int
    source: str  # "readings" | "placeholder"


def seal_from_readings(
    readings_records: list[dict],
    *,
    device_pubkey: str | None,
) -> SealInputs | None:
    """Derive season_root + verified_hours from published hour batches.

    Returns None if there are no usable hour records (caller falls back to
    the uptime-placeholder path).
    """
    if not readings_records:
        return None

    hour_roots: dict[int, str] = {}
    by_hour: dict[int, list[dict]] = {}
    reading_count = 0

    for rec in readings_records:
        try:
            hour = int(rec["hour"])
        except (KeyError, TypeError, ValueError):
            continue
        readings = rec.get("readings") or []
        if not isinstance(readings, list):
            continue
        # Prefer recomputed hour_root so a corrupted stored root can't seal.
        recomputed = schema.hour_root(readings)
        stored = rec.get("hour_root")
        if stored and stored != recomputed:
            # Still use recomputed — verifiers will recompute too.
            pass
        hour_roots[hour] = recomputed
        by_hour[hour] = readings
        reading_count += len(readings)

    if not hour_roots:
        return None

    season_root = schema.season_root(hour_roots)
    if device_pubkey:
        verified = schema.verified_hours(by_hour, device_pubkey)
    else:
        # No pubkey → cannot verify device sigs; count hours with ≥1 reading.
        verified = sum(1 for rs in by_hour.values() if rs)

    return SealInputs(
        season_root=season_root,
        verified_hours=verified,
        reading_count=reading_count,
        hour_count=len(hour_roots),
        source="readings",
    )


def load_season_readings(
    rpc,
    store_id: str,
    *,
    node_id: str,
    season: int,
) -> list[dict]:
    """Fetch all ``readings:<node>:<season>:*`` values from DataLayer.

    Soft-fails to [] on any RPC/key error so the attest writer can fall
    back to the placeholder path without aborting the whole run.
    """
    from .fetch import _discover_hours  # shared hour discovery

    node_id = node_id.upper()
    try:
        hours = _discover_hours(rpc, store_id, node_id, season)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for h in hours:
        try:
            raw = rpc.get_value(store_id, schema.readings_key(node_id, season, h))
        except Exception:  # noqa: BLE001
            continue
        rec = schema.parse_value(raw)
        if rec:
            out.append(rec)
    return out
