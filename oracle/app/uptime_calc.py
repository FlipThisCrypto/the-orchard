# SPDX-License-Identifier: Apache-2.0
"""Authoritative uptime computation.

Shared by the ``/uptime`` query and the attestation integrity cross-check so
both agree by construction — the oracle's answer to "how many hours was this
Tree online in this Season?" has exactly one implementation.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, seasons


def hours_online_for(db: Session, node_id: str, season: int) -> tuple[int, list[str]]:
    """(hours_online, sorted hit buckets) for a node·season from uptime_hours.

    hours_online = number of distinct UTC-hour buckets within the Season that
    hold at least one reading.
    """
    season_buckets = set(seasons.hour_buckets_in_season(season))
    rows = (
        db.execute(
            select(models.UptimeHour.hour_utc).where(
                models.UptimeHour.node_id == node_id.upper(),
                models.UptimeHour.hour_utc.in_(season_buckets),
            )
        )
        .scalars()
        .all()
    )
    hit = sorted(set(rows))
    return len(hit), hit
