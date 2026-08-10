# SPDX-License-Identifier: Apache-2.0
"""$JUICE economics — the single source of truth for reward emission.

Canonical specification: ``docs/token/JUICE.md``.

    from orchard_chia.economics import (
        TreeDay, calculate_daily_rewards, daily_ceiling_mojos, apply_distribution)

    ceiling = daily_ceiling_mojos(day_index=0, pool_remaining_mojos=POOL)
    result  = calculate_daily_rewards(trees, ceiling.ceiling_mojos)
    state   = apply_distribution(POOL, ceiling, result.distributed_mojos)

WHAT SUPERSEDES WHAT
====================

This model replaces two earlier ones. Both remain in the tree, marked, because
records they produced are on chain and a reader needs to be able to reconstruct
what was computed at the time:

  * ``orchard_chia.payout`` — "1 $JUICE per Tree per day". Per-Tree accrual with
    no ceiling at all: 10,000 Trees would have minted 10,000 tokens a day. It is
    what every attestation currently on the store was scored under.
  * ``orchard_chia.allocation`` — a fixed budget split by each WALLET's mean
    Tree uptime. Bounded, and sybil-resistant, but it weighted wallets rather
    than Trees and so penalised an operator for running a second Tree.

The model here is the reconciliation of the two: a fixed NETWORK ceiling that
more Trees can only divide, with weight attaching to Trees rather than wallets.

THE ONE-LINE VERSION
====================

The network has a maximum it may emit today. Trees divide it in proportion to
their sensors, each takes the fraction of its slice that its verified uptime
earned, and everything unearned stays in the pool and extends the runway.
"""
from __future__ import annotations

from .constants import (BASE_SENSOR_WEIGHT, DAILY_EMISSION_MOJOS_BY_YEAR,
                        DAYS_PER_EMISSION_YEAR, HEARTBEATS_PER_DAY,
                        LIQUIDITY_MOJOS, MAX_SENSOR_WEIGHT,
                        MIN_QUALIFYING_SENSORS, MODEL_VERSION,
                        MOJOS_PER_JUICE, SCHEDULE_YEARS,
                        SENSOR_BONUS_PER_EXTRA, TERMINAL_DAILY_EMISSION_MOJOS,
                        TOTAL_SUPPLY_MOJOS, TREE_REWARDS_POOL_MOJOS,
                        format_juice, juice)
from .emission import (DailyCeiling, EmissionError, PoolState,
                       apply_distribution, daily_ceiling_mojos, is_exhausted,
                       emission_year_for_day, pool_after,
                       runway_days_remaining, schedule_total_mojos,
                       scheduled_daily_mojos)
from .rewards import (DailyRewards, RewardError, TreeDay, TreeReward,
                      calculate_daily_rewards, sensor_weight)

__all__ = [
    "MODEL_VERSION", "MOJOS_PER_JUICE", "TOTAL_SUPPLY_MOJOS",
    "TREE_REWARDS_POOL_MOJOS", "LIQUIDITY_MOJOS", "HEARTBEATS_PER_DAY",
    "DAILY_EMISSION_MOJOS_BY_YEAR", "TERMINAL_DAILY_EMISSION_MOJOS",
    "SCHEDULE_YEARS", "DAYS_PER_EMISSION_YEAR", "SENSOR_BONUS_PER_EXTRA",
    "MAX_SENSOR_WEIGHT", "BASE_SENSOR_WEIGHT", "MIN_QUALIFYING_SENSORS",
    "juice", "format_juice",
    "DailyCeiling", "PoolState", "EmissionError", "daily_ceiling_mojos",
    "apply_distribution", "emission_year_for_day", "scheduled_daily_mojos",
    "schedule_total_mojos", "runway_days_remaining", "pool_after",
    "is_exhausted",
    "TreeDay", "TreeReward", "DailyRewards", "RewardError",
    "calculate_daily_rewards", "sensor_weight",
]
