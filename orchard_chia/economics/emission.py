# SPDX-License-Identifier: Apache-2.0
"""The daily network emission ceiling, and the pool it comes out of.

Two ideas, and the second is the one that makes the first honest:

  * There is a maximum the WHOLE NETWORK may emit on a given day. Trees divide
    it. More Trees never raise it.
  * Whatever is not earned is not emitted. It stays in the Tree Rewards Pool
    and extends the runway. Not burned, not redistributed to Trees that were
    up, not swept to liquidity or treasury — simply not spent.

The second rule is why the eight-year schedule is a floor on the programme's
life rather than an expiry date. A network at 60% uptime does not emit 85M over
eight years; it emits 51M and keeps going.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (DAILY_EMISSION_MOJOS_BY_YEAR, DAYS_PER_EMISSION_YEAR,
                        SCHEDULE_YEARS, TERMINAL_DAILY_EMISSION_MOJOS,
                        TREE_REWARDS_POOL_MOJOS)


class EmissionError(ValueError):
    pass


def emission_year_for_day(day_index: int) -> int:
    """Which schedule year a day falls in. ``day_index`` is 0-based from genesis.

    Returns a year beyond 8 for days past the schedule; callers should use
    :func:`daily_ceiling_mojos`, which handles the terminal rate. Kept separate
    so a report can say "year 11" rather than silently claiming year 8.
    """
    if day_index < 0:
        raise EmissionError(
            f"day_index {day_index} precedes network genesis. A negative day is "
            f"a clock or configuration error, and guessing a rate for it would "
            f"emit tokens for a day that never happened.")
    return day_index // DAYS_PER_EMISSION_YEAR + 1


def scheduled_daily_mojos(year: int) -> int:
    """The schedule's ceiling for a year, with the terminal rate past year 8."""
    if year < 1:
        raise EmissionError(f"emission year must be >= 1, got {year}")
    if year <= SCHEDULE_YEARS:
        return DAILY_EMISSION_MOJOS_BY_YEAR[year]
    return TERMINAL_DAILY_EMISSION_MOJOS


@dataclass(frozen=True)
class DailyCeiling:
    """What may be emitted today, and why it is that number."""
    day_index: int
    year: int
    scheduled_mojos: int
    pool_remaining_mojos: int
    ceiling_mojos: int
    limited_by_pool: bool

    @property
    def past_schedule(self) -> bool:
        return self.year > SCHEDULE_YEARS


def daily_ceiling_mojos(day_index: int, pool_remaining_mojos: int) -> DailyCeiling:
    """The most the network may emit today.

    The schedule's rate, unless the pool holds less — in which case the pool IS
    the ceiling. Rewards stop when it reaches zero; the balance never goes
    negative, and the last day pays out exactly what is left rather than
    overspending to hit a rate.
    """
    if pool_remaining_mojos < 0:
        raise EmissionError(
            f"pool balance is negative ({pool_remaining_mojos}). That is not a "
            f"state this system can reach by paying rewards, so something has "
            f"corrupted the ledger — refusing to emit against it.")
    year = emission_year_for_day(day_index)
    scheduled = scheduled_daily_mojos(year)
    ceiling = min(scheduled, pool_remaining_mojos)
    return DailyCeiling(
        day_index=day_index, year=year, scheduled_mojos=scheduled,
        pool_remaining_mojos=pool_remaining_mojos, ceiling_mojos=ceiling,
        limited_by_pool=ceiling < scheduled,
    )


@dataclass(frozen=True)
class PoolState:
    """The Tree Rewards Pool after a day's distribution.

    ``unearned_mojos`` is the headline number of this whole model: emission
    that was available and was not earned, which stays put and extends the
    runway.
    """
    opening_mojos: int
    ceiling_mojos: int
    distributed_mojos: int
    closing_mojos: int
    unearned_mojos: int


def apply_distribution(pool_remaining_mojos: int, ceiling: DailyCeiling,
                       distributed_mojos: int) -> PoolState:
    """Settle a day against the pool.

    Refuses to overspend rather than clamping quietly. A distribution above the
    ceiling means the reward calculation is wrong, and silently trimming it
    would hide the bug while still paying a number nobody computed.
    """
    if distributed_mojos < 0:
        raise EmissionError(f"distributed {distributed_mojos} mojos — negative")
    if distributed_mojos > ceiling.ceiling_mojos:
        raise EmissionError(
            f"distribution {distributed_mojos} exceeds today's ceiling "
            f"{ceiling.ceiling_mojos}. Refusing to trim it: an over-ceiling "
            f"total means the reward calculation is wrong, and clamping would "
            f"pay out a number no rule produced.")
    closing = pool_remaining_mojos - distributed_mojos
    if closing < 0:
        raise EmissionError("distribution would drive the pool negative")
    return PoolState(
        opening_mojos=pool_remaining_mojos,
        ceiling_mojos=ceiling.ceiling_mojos,
        distributed_mojos=distributed_mojos,
        closing_mojos=closing,
        unearned_mojos=ceiling.ceiling_mojos - distributed_mojos,
    )


def is_exhausted(pool_remaining_mojos: int, eligible_tree_count: int) -> bool:
    """Whether the reward programme is over.

    NOT `pool == 0`, and not "today paid nothing" either. Both are wrong in
    ways worth stating, because a scheduler has to decide when to stop.

    Every reward is floored, so a day can distribute zero while the pool still
    holds mojos. Simulated at 75% uptime with four Trees: the pool depletes on
    day 5,340 and then sits at 4 mojos, paying nothing, forever. It looks
    finished.

    It is not. Those 4 mojos are still earnable — if all four Trees reach 24/24,
    each earns floor(4 x 1/4 x 1) = 1 mojo and the pool empties exactly. The
    stall is a consequence of sustained sub-100% uptime, not a dead end, and
    treating it as the end would write off rewards operators can still earn.

    So exhaustion is the stronger condition: the pool cannot fund a single mojo
    for a single Tree even at perfect uptime. Below that, nothing any operator
    does can extract another mojo, and the programme really is over.

    A scheduler should stop on this. A scheduler stopping on "today paid
    nothing" would end the programme during a quiet week.
    """
    if pool_remaining_mojos <= 0:
        return True
    if eligible_tree_count <= 0:
        return False        # nothing earnable today, but the pool is intact
    # At full uptime the smallest share is pool // n. Once that floors to zero,
    # no uptime and no sensor weighting can produce a payable amount.
    return pool_remaining_mojos // eligible_tree_count < 1


def schedule_total_mojos(years: int = SCHEDULE_YEARS) -> int:
    """What the schedule emits if every scheduled reward is earned in full.

    Should land on the Tree Rewards Pool almost exactly — that is what makes
    eight years the intended runway. Asserted by the tests rather than assumed.
    """
    return sum(scheduled_daily_mojos(y) * DAYS_PER_EMISSION_YEAR
               for y in range(1, years + 1))


def runway_days_remaining(pool_remaining_mojos: int, day_index: int) -> int:
    """Whole days the pool could still fund at the current ceiling.

    A planning aid, not a promise: real uptime is below 100%, so the true
    runway is longer than this. Deliberately pessimistic in that direction.
    """
    if pool_remaining_mojos <= 0:
        return 0
    rate = scheduled_daily_mojos(emission_year_for_day(day_index))
    return pool_remaining_mojos // rate


def pool_after(distributed_total_mojos: int) -> int:
    """Remaining pool given everything distributed so far."""
    if distributed_total_mojos < 0:
        raise EmissionError("cumulative distribution cannot be negative")
    if distributed_total_mojos > TREE_REWARDS_POOL_MOJOS:
        raise EmissionError(
            f"cumulative distribution {distributed_total_mojos} exceeds the "
            f"Tree Rewards Pool {TREE_REWARDS_POOL_MOJOS}. The fixed-supply "
            f"invariant has been broken.")
    return TREE_REWARDS_POOL_MOJOS - distributed_total_mojos
