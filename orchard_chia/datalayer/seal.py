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


class SealReadError(RuntimeError):
    """The published readings for a node·season could not be read completely.

    Distinct from "there are none": sealing on incomplete data would sign a
    permanent, wrong conclusion.
    """


@dataclass(frozen=True)
class SealInputs:
    """Material used to seal one node·season."""
    season_root: str
    verified_hours: int
    # The per-hour signature quorum verified_hours was computed under.
    # Travels with the seal so the writer records the rule it used.
    min_readings_per_hour: int
    reading_count: int
    hour_count: int
    source: str  # "readings" | "placeholder"
    # Hours whose stored hour_root disagreed with a recompute over their
    # readings — a published-data corruption/tampering signal. >0 is a red flag.
    root_mismatches: int = 0
    # True when verified_hours was derived by checking device signatures against
    # a known pubkey. False means it is a reading-PRESENCE count (no pubkey was
    # available) and must not be presented as cryptographically verified.
    sigs_verified: bool = True


def seal_from_readings(
    readings_records: list[dict],
    *,
    device_pubkey: str | None,
    min_readings_per_hour: int = schema.MIN_VERIFIED_READINGS_PER_HOUR,
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
    root_mismatches = 0

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
            # Still use recomputed — verifiers will recompute too — but count
            # it: the published readings don't match their published root.
            root_mismatches += 1
        hour_roots[hour] = recomputed
        by_hour[hour] = readings
        reading_count += len(readings)

    if not hour_roots:
        return None

    season_root = schema.season_root(hour_roots)
    if device_pubkey:
        verified = schema.verified_hours(by_hour, device_pubkey,
                                        min_readings=min_readings_per_hour)
        sigs_verified = True
    else:
        # No pubkey → cannot verify device sigs; count hours with ≥1 reading.
        # This is a PRESENCE count, not a signature-verified one — flagged so
        # callers don't present it as cryptographically verified.
        verified = sum(1 for rs in by_hour.values()
                       if len(rs) >= min_readings_per_hour)
        sigs_verified = False

    return SealInputs(
        season_root=season_root,
        verified_hours=verified,
        min_readings_per_hour=min_readings_per_hour,
        reading_count=reading_count,
        hour_count=len(hour_roots),
        source="readings",
        root_mismatches=root_mismatches,
        sigs_verified=sigs_verified,
    )


def load_season_readings(
    rpc,
    store_id: str,
    *,
    node_id: str,
    season: int,
    strict: bool = False,
) -> list[dict]:
    """Fetch all ``readings:<node>:<season>:*`` values from DataLayer.

    ``strict=False`` (default, for read-only reporting like reconcile) soft-fails
    to ``[]`` / skips unreadable hours.

    ``strict=True`` raises :class:`SealReadError` if the store cannot be listed
    or ANY discovered hour cannot be read. Anything that SIGNS a conclusion from
    this data must use strict mode: otherwise a transient RPC failure is
    indistinguishable from "nothing was published", and the writer permanently
    seals either a placeholder (claiming nothing exists) or a ``season_root``
    over a PARTIAL set with an understated ``verified_hours`` — both signed,
    both labelled proof-backed, both irreversible.
    """
    from .fetch import _discover_hours  # shared hour discovery

    node_id = node_id.upper()
    try:
        if strict:
            hours = _discover_hours(rpc, store_id, node_id, season, strict=True)
        else:
            hours = _discover_hours(rpc, store_id, node_id, season)
    except SealReadError:
        raise
    except Exception as e:  # noqa: BLE001
        if strict:
            raise SealReadError(
                f"cannot list store keys for {node_id}:{season}: {e}"
            ) from e
        return []

    out: list[dict] = []
    for h in hours:
        key = schema.readings_key(node_id, season, h)
        try:
            raw = rpc.get_value_strict(store_id, key) if strict else rpc.get_value(store_id, key)
        except Exception as e:  # noqa: BLE001
            if strict:
                raise SealReadError(
                    f"hour {h:02d} of {node_id}:{season} is unreadable: {e}"
                ) from e
            continue
        rec = schema.parse_value(raw)
        if rec:
            out.append(rec)
        elif strict:
            raise SealReadError(
                f"hour {h:02d} of {node_id}:{season} did not parse as a readings record"
            )
    return out
