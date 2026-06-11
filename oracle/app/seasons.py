# SPDX-License-Identifier: Apache-2.0
"""Season math.

v1 uses **day-aligned UTC Seasons**:

    Season 1 = the 24-hour UTC day starting at `season_genesis_date` 00:00Z
    Season 2 = the next 24h, etc.

This is a deliberate simplification so we can track and reward uptime
without depending on a synced Chia full node. Phase 5 will replace this
module with Chia-block-aligned Seasons (4608 blocks each) and the rest
of the oracle keeps working — only this module knows how to map between
wall-clock time and Season numbers.

The hour-bucket format stays the same in v1 and v2: ``"YYYY-MM-DDTHH"``
UTC. Uptime math is ``count(distinct hour buckets) within season window``.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .config import settings


def season_number_for(ts: datetime) -> int:
    """Which Season is `ts` part of?"""
    genesis_dt = datetime.combine(settings().season_genesis_date, time.min, tzinfo=timezone.utc)
    delta_days = (ts.astimezone(timezone.utc) - genesis_dt).days
    return 1 + max(0, delta_days)


def season_bounds(season_number: int) -> tuple[datetime, datetime]:
    """Inclusive start, exclusive end of `season_number` in UTC."""
    if season_number < 1:
        raise ValueError("season_number must be >= 1")
    genesis_dt = datetime.combine(settings().season_genesis_date, time.min, tzinfo=timezone.utc)
    start = genesis_dt + timedelta(days=season_number - 1)
    end = start + timedelta(days=1)
    return start, end


def hour_bucket_for(ts: datetime) -> str:
    """Return the YYYY-MM-DDTHH UTC bucket string for `ts`."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def hour_buckets_in_season(season_number: int) -> list[str]:
    """All 24 hour-bucket strings that belong to `season_number`."""
    start, _ = season_bounds(season_number)
    return [hour_bucket_for(start + timedelta(hours=h)) for h in range(24)]


def current_season() -> int:
    return season_number_for(datetime.now(timezone.utc))
