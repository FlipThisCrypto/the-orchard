# SPDX-License-Identifier: Apache-2.0
"""Bridge: oracle observations -> TreeDay -> wallet payables.

The allocation service (collector, planner, executor, audit, run-lock) is
retained infrastructure; only its arithmetic was superseded. This module is the
new arithmetic in the old socket: it turns what the collector observed into
``TreeDay`` inputs, runs the ratified reward calculation, and hands the planner
per-wallet totals it can turn into unsigned instructions.

The mapping choices, each of which changes money and is therefore written down:

  * heartbeats  <- distinct hours with >= 1 accepted reading, capped at 24.
    The oracle's ``hours_online`` for a day IS the heartbeat count under the
    24-windows model. When the per-hour signature quorum lands end-to-end this
    becomes "hours meeting the quorum" with no shape change here.
  * sensors     <- count of DECLARED sensor names, until approved sensor
    classes exist. A Tree declaring none gets weight zero and earns nothing.
  * eligibility <- the collector's exclusions (stale, never-reported, no
    wallet) carry over verbatim, reason and all.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import HEARTBEATS_PER_DAY, MODEL_VERSION
from .emission import DailyCeiling, apply_distribution, daily_ceiling_mojos
from .rewards import DailyRewards, TreeDay, calculate_daily_rewards


def tree_day_from_observation(
    *,
    tree_id: str,
    wallet_address: str | None,
    declared_sensors: list[str] | None,
    hours_with_readings: int,
    eligible: bool = True,
    ineligible_reason: str | None = None,
) -> TreeDay:
    """One oracle observation as the reward calculation's input.

    Hours are clamped to 0..24 rather than refused: the oracle reports season
    hours which can exceed a day near boundaries, and an out-of-range claim
    from upstream must cap a Tree's own reward, never crash everyone's run.
    """
    beats = max(0, min(int(hours_with_readings), HEARTBEATS_PER_DAY))
    sensors = [s for s in (declared_sensors or []) if s]
    if eligible and not wallet_address:
        # The reward layer refuses an eligible Tree with no wallet (it cannot
        # be settled). Downgrade to ineligible-with-reason so the day still
        # settles for everyone else and the audit shows why this Tree did not.
        eligible = False
        ineligible_reason = ineligible_reason or "no wallet_address visible"
    return TreeDay(
        tree_id=tree_id.upper(),
        wallet_address=wallet_address or "",
        qualifying_sensors=len(set(sensors)),
        verified_heartbeats=beats,
        eligible=eligible,
        ineligible_reason=ineligible_reason,
    )


@dataclass(frozen=True)
class Settlement:
    """A settled day: what each wallet is owed, and the pool bookkeeping."""
    model_version: str
    day_index: int
    ceiling: DailyCeiling
    rewards: DailyRewards
    pool_opening_mojos: int
    pool_closing_mojos: int

    @property
    def payable_by_wallet(self) -> dict[str, int]:
        return self.rewards.by_wallet()

    @property
    def distributed_mojos(self) -> int:
        return self.rewards.distributed_mojos

    @property
    def unearned_mojos(self) -> int:
        return self.rewards.unearned_mojos


def settle_day(trees: list[TreeDay], *, day_index: int,
               pool_remaining_mojos: int) -> Settlement:
    """The full daily pipeline: ceiling -> rewards -> pool.

    Pure. The caller (the allocation service's cycle runner) owns persistence,
    locking, idempotency and the spend itself — exactly the parts of that
    service that were worth keeping.
    """
    ceiling = daily_ceiling_mojos(day_index, pool_remaining_mojos)
    rewards = calculate_daily_rewards(trees, ceiling.ceiling_mojos)
    state = apply_distribution(pool_remaining_mojos, ceiling,
                               rewards.distributed_mojos)
    return Settlement(
        model_version=MODEL_VERSION,
        day_index=day_index,
        ceiling=ceiling,
        rewards=rewards,
        pool_opening_mojos=state.opening_mojos,
        pool_closing_mojos=state.closing_mojos,
    )
