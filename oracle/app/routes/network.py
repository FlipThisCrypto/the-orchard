# SPDX-License-Identifier: Apache-2.0
"""Network-wide aggregate stats (Phase 6.6).

Powers the home-page card on a public-mode dashboard. The card shows
the network as a single number per dimension — Trees, Readings,
Attestations on chain — rather than the per-Tree table that operators
see on their own oracle.

This endpoint is intentionally PUBLIC (no auth) — it's the data we
want strangers to see when they're deciding whether to operate a
Tree of their own. By design it returns ONLY aggregates: no
wallet_addresses, no GPS, no node_ids, no per-tree breakdown.
Anything that could deanonymize an operator stays inside their own
oracle's scoped routes.

Numbers are computed lazily on each request (SQLite COUNTs are
cheap at this scale). For larger deployments we'd cache or
materialize, but premature optimization for v1.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, seasons
from ..db import get_db

router = APIRouter()

# This endpoint is public, unauthenticated, and unrate-limited, and each call
# runs full-table COUNTs (including COUNT(readings), which grows without bound).
# A short single-flight cache bounds the DB load regardless of request rate —
# neutralizing both the scale cost and a cheap DoS-amplification vector — while
# keeping the home-page card near-fresh. Set ORCHARD_NETWORK_STATS_TTL_S=0 to
# disable (used by the test suite so per-request assertions see live counts).
_lock = threading.Lock()
_cache: "NetworkStats | None" = None
_cache_mono = 0.0


def _ttl_seconds() -> float:
    try:
        return float(os.environ.get("ORCHARD_NETWORK_STATS_TTL_S", "30") or "30")
    except ValueError:
        return 30.0


def reset_cache_for_tests() -> None:
    global _cache, _cache_mono
    with _lock:
        _cache = None
        _cache_mono = 0.0


class NetworkStats(BaseModel):
    """Aggregate snapshot of the network. All counts are non-negative
    integers — UInt-ish. Timestamps are ISO 8601 UTC."""

    trees_registered:       int = Field(..., description="Total Trees ever registered")
    trees_active_24h:       int = Field(..., description="Trees with at least one reading in the past 24h")
    readings_total:         int = Field(..., description="Total readings stored across all Trees")
    readings_last_24h:      int = Field(..., description="Readings received in the past 24h")
    attestations_total:     int = Field(..., description="On-chain attestations recorded by the writer")
    # Pipeline liveness. Readings flowing while attestation goes quiet is the
    # one failure the outside world could never see: exit codes and ops
    # journals live on the operator's box, so a stalled writer looked exactly
    # like a healthy quiet one. An external heartbeat can now alert on
    # "last_attestation_at is old while readings_last_24h is not".
    last_attestation_at:    str | None = Field(
        None, description="When the writer last recorded an on-chain attestation")
    # Rejections, so "device quiet" and "oracle refusing" are distinguishable
    # from outside. A healthy network rejects ~nothing; readings_last_24h
    # falling while this rises means the oracle is refusing posts (and the
    # reasons say why); both falling means devices went quiet.
    readings_rejected_24h:  int = Field(
        0, description="Refused ingest attempts, today + yesterday UTC")
    reject_reasons_24h:     dict[str, int] = Field(
        default_factory=dict, description="Refusals by reason, same window")
    last_reading_at:        str | None = Field(
        None, description="When any Tree last posted an accepted reading")
    current_season:         int = Field(..., description="Current Season number per oracle's clock")
    as_of_utc:              str = Field(..., description="When this snapshot was taken (server clock)")


@router.get("/network/stats", response_model=NetworkStats)
def network_stats(db: Session = Depends(get_db)) -> NetworkStats:
    """Public aggregate stats for the home-page card.

    No auth — by design. Caller can't infer per-operator data from
    these counts. Served from a short single-flight cache (see module top).
    """
    global _cache, _cache_mono
    ttl = _ttl_seconds()
    if ttl <= 0:
        return _compute_stats(db)
    now_mono = time.monotonic()
    with _lock:
        if _cache is not None and (now_mono - _cache_mono) < ttl:
            return _cache
        stats = _compute_stats(db)
        _cache = stats
        _cache_mono = now_mono
        return stats


def _compute_stats(db: Session) -> NetworkStats:
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Retired Trees are excluded from every count that describes the network.
    # They are not deleted — their readings, uptime and attestations all remain
    # — but a ghost from a re-flash, or a board sitting on a shelf awaiting
    # sensors, is not part of the living Orchard and must not inflate it. The
    # network claiming more Trees than it has is the same class of dishonesty
    # as claiming more uptime than it can prove.
    live = models.Node.retired_at.is_(None)

    trees_registered = db.execute(
        select(func.count(models.Node.node_id)).where(live)
    ).scalar_one()

    # "Active" = had at least one reading land in the past 24h.
    # last_reading_at is updated on every reading, so this is cheap.
    trees_active_24h = db.execute(
        select(func.count(models.Node.node_id))
        .where(live)
        .where(models.Node.last_reading_at >= cutoff_24h)
    ).scalar_one()

    readings_total = db.execute(
        select(func.count(models.Reading.id))
    ).scalar_one()

    readings_last_24h = db.execute(
        select(func.count(models.Reading.id))
        .where(models.Reading.received_at >= cutoff_24h)
    ).scalar_one()

    attestations_total = db.execute(
        select(func.count(models.Attestation.id))
    ).scalar_one()

    last_att = db.execute(
        select(func.max(models.Attestation.written_to_datalayer_at))
    ).scalar_one()
    last_reading = db.execute(
        select(func.max(models.Reading.received_at))
    ).scalar_one()

    from datetime import timedelta as _td
    days = [(now.strftime("%Y-%m-%d")), ((now - _td(days=1)).strftime("%Y-%m-%d"))]
    reject_rows = db.execute(
        select(models.RejectCounter.reason,
               func.sum(models.RejectCounter.count))
        .where(models.RejectCounter.day_utc.in_(days))
        .group_by(models.RejectCounter.reason)
    ).all()
    reject_reasons = {r: int(c) for r, c in reject_rows}

    return NetworkStats(
        trees_registered=trees_registered,
        trees_active_24h=trees_active_24h,
        readings_total=readings_total,
        readings_last_24h=readings_last_24h,
        attestations_total=attestations_total,
        last_attestation_at=last_att.isoformat() if last_att else None,
        readings_rejected_24h=sum(reject_reasons.values()),
        reject_reasons_24h=reject_reasons,
        last_reading_at=last_reading.isoformat() if last_reading else None,
        current_season=seasons.current_season(),
        as_of_utc=now.isoformat(),
    )
