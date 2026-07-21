# SPDX-License-Identifier: Apache-2.0
"""Closed-hour publish scheduling (SPEC §6 hot path).

The publisher must write **completed** UTC hours only — never the still-
open hour whose readings may still arrive. Watermark + oracle window
queries keep re-runs cheap and convergent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


# Matches oracle default season_genesis_date so publisher and oracle agree
# without importing oracle.app (different package roots / install layouts).
DEFAULT_SEASON_GENESIS = date(2026, 5, 27)


@dataclass(frozen=True)
class ClosedHour:
    """One fully closed UTC hour bucket."""
    season: int
    hour: int  # 0–23 within the Season day
    start_ms: int  # inclusive epoch millis
    end_ms: int  # exclusive epoch millis
    start_utc: datetime

    @property
    def key(self) -> tuple[int, int]:
        return (self.season, self.hour)


def season_number_for(
    ts: datetime,
    *,
    genesis: date = DEFAULT_SEASON_GENESIS,
) -> int:
    """Day-aligned Season number (same rule as oracle seasons.py)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    genesis_dt = datetime(genesis.year, genesis.month, genesis.day, tzinfo=timezone.utc)
    delta_days = (ts - genesis_dt).days
    return 1 + max(0, delta_days)


def floor_hour_utc(ts: datetime) -> datetime:
    """Truncate to the start of the UTC hour containing ``ts``."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=0, second=0, microsecond=0)


def last_closed_hour_start(now: datetime | None = None) -> datetime:
    """Start of the most recently *closed* UTC hour.

    At 13:05Z the closed hour is 12:00–13:00, so this returns 12:00Z.
    At exactly 13:00:00 the hour 12:00–13:00 just closed → still 12:00Z.
    """
    now = now or datetime.now(timezone.utc)
    floored = floor_hour_utc(now)
    # If we are exactly on the hour boundary, the previous hour just closed.
    # floor_hour_utc(13:00) == 13:00, but the open hour is 13:00–14:00, so
    # last closed starts at 12:00. Subtract one hour always: the current
    # open hour's start is ``floored``; last closed is floored - 1h.
    return floored - timedelta(hours=1)


def closed_hour_for_start(
    start: datetime,
    *,
    genesis: date = DEFAULT_SEASON_GENESIS,
) -> ClosedHour:
    """Build a ClosedHour for an hour that begins at ``start`` (UTC hour floor)."""
    start = floor_hour_utc(start)
    end = start + timedelta(hours=1)
    season = season_number_for(start, genesis=genesis)
    return ClosedHour(
        season=season,
        hour=start.hour,
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
        start_utc=start,
    )


def iter_closed_hours(
    *,
    lookback_hours: int = 48,
    now: datetime | None = None,
    genesis: date = DEFAULT_SEASON_GENESIS,
    through: datetime | None = None,
) -> list[ClosedHour]:
    """Closed hours from ``last_closed - (lookback-1)`` through ``last_closed``.

    Oldest first. Caps lookback to avoid unbounded oracle scans.
    """
    if lookback_hours < 1:
        return []
    last = through or last_closed_hour_start(now)
    last = floor_hour_utc(last)
    # Ensure last is not the open hour if caller passed ``now`` as through.
    open_start = floor_hour_utc(now or datetime.now(timezone.utc))
    if last >= open_start:
        last = open_start - timedelta(hours=1)

    out: list[ClosedHour] = []
    for i in range(lookback_hours - 1, -1, -1):
        start = last - timedelta(hours=i)
        out.append(closed_hour_for_start(start, genesis=genesis))
    return out


def is_closed_hour(
    season: int,
    hour: int,
    *,
    now: datetime | None = None,
    genesis: date = DEFAULT_SEASON_GENESIS,
) -> bool:
    """True if (season, hour) is fully in the past relative to ``now``."""
    last = last_closed_hour_start(now)
    ch = closed_hour_for_start(last, genesis=genesis)
    if season < ch.season:
        return True
    if season > ch.season:
        return False
    return hour <= ch.hour
