# SPDX-License-Identifier: Apache-2.0
"""The canonical daily reward calculation. Pure, exact, deterministic.

THE FORMULA
===========

    sensor_weight    = min(1 + 0.05 * (qualifying_sensors - 1), 1.25)
    total_weight     = sum(sensor_weight for every eligible Tree)
    potential        = daily_ceiling * sensor_weight / total_weight
    uptime_factor    = verified_heartbeats / 24
    tree_reward      = floor(potential * uptime_factor)          # mojos

    distributed      = sum(tree_reward)
    unearned         = daily_ceiling - distributed               # stays in pool

Rewards belong to TREES, not wallets. A wallet owning three Trees is three
participants; a wallet owning one is one. Splitting Trees across wallets
changes nothing, and consolidating them changes nothing — which is what makes
the wallet layer irrelevant to emission and therefore not worth gaming.

WHY EVERY DIVISION IS A FRACTION
================================

The share arithmetic is exact rationals down to a single floor per Tree. Three
Trees splitting a pool three ways is 1/3 each, and no binary float represents
that; a chain of float multiplications would make the result depend on the
order Trees came out of a database. Two independent implementations given the
same inputs must produce byte-identical outputs, or "verifiable" means nothing.

WHY FLOOR, NOT ROUND
====================

Every Tree's reward rounds DOWN, and the remainder is not redistributed. That
is not a rounding convenience — it is the model's central rule expressed in
arithmetic. Rounding up, or handing the remainder to whoever had the largest
fractional part, would emit mojos nobody earned. Anything not earned stays in
the pool and extends the runway. The sum can therefore never exceed the
ceiling, which is the invariant the fixed supply rests on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .constants import (BASE_SENSOR_WEIGHT, HEARTBEATS_PER_DAY,
                        MAX_SENSOR_WEIGHT, MIN_QUALIFYING_SENSORS,
                        MODEL_VERSION, SENSOR_BONUS_PER_EXTRA)


class RewardError(ValueError):
    pass


def sensor_weight(qualifying_sensors: int) -> Fraction:
    """A Tree's share multiplier from its sensor count.

    1.00 at one sensor, +0.05 each, capped at 1.25 from six. Exact twentieths:
    1.05 is not representable in binary floating point and this number is
    multiplied into every reward.

    Weighting redistributes the fixed daily pool. It cannot enlarge it, so a
    Tree adding sensors takes a larger slice of the same cake rather than
    baking more — which is why the cap can be generous without risking supply.
    """
    if not isinstance(qualifying_sensors, int) or isinstance(qualifying_sensors, bool):
        raise RewardError(
            f"qualifying_sensors must be an int, got "
            f"{type(qualifying_sensors).__name__}")
    if qualifying_sensors < 0:
        raise RewardError(f"qualifying_sensors cannot be negative: {qualifying_sensors}")
    if qualifying_sensors < MIN_QUALIFYING_SENSORS:
        return Fraction(0)
    weight = BASE_SENSOR_WEIGHT + SENSOR_BONUS_PER_EXTRA * (qualifying_sensors - 1)
    return min(weight, MAX_SENSOR_WEIGHT)


@dataclass(frozen=True)
class TreeDay:
    """One Tree's day, as the reward calculation sees it.

    ``eligible`` is decided upstream by the eligibility layer (registration,
    ownership, Pass, device signatures, replay protection, duplicate identity).
    This module does not re-derive those rules; it refuses to reward a Tree
    that failed them, and records why.
    """
    tree_id: str
    wallet_address: str
    qualifying_sensors: int
    verified_heartbeats: int
    eligible: bool = True
    ineligible_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.tree_id:
            raise RewardError("tree_id is required")
        if not isinstance(self.verified_heartbeats, int) or isinstance(
                self.verified_heartbeats, bool):
            raise RewardError(
                f"{self.tree_id}: verified_heartbeats must be an int")
        if not (0 <= self.verified_heartbeats <= HEARTBEATS_PER_DAY):
            raise RewardError(
                f"{self.tree_id}: {self.verified_heartbeats} heartbeats is "
                f"outside 0..{HEARTBEATS_PER_DAY}. A day cannot contain more "
                f"reward windows than it has hours, and accepting one would let "
                f"a burst of heartbeats buy more than a day of uptime.")
        if self.eligible and not self.wallet_address:
            raise RewardError(
                f"{self.tree_id} is eligible but has no wallet to pay. Rewards "
                f"are earned by Trees and delivered to wallets; a Tree without "
                f"one cannot be settled and must not silently reduce everyone "
                f"else's share by inflating total_weight.")

    @property
    def uptime_factor(self) -> Fraction:
        return Fraction(self.verified_heartbeats, HEARTBEATS_PER_DAY)


@dataclass(frozen=True)
class TreeReward:
    tree_id: str
    wallet_address: str
    sensor_weight: Fraction
    verified_heartbeats: int
    uptime_factor: Fraction
    potential_mojos: int          # what full uptime would have earned
    reward_mojos: int             # what it actually earned
    share: Fraction               # of total network weight

    @property
    def forfeited_mojos(self) -> int:
        """Earned nothing through downtime. Stays in the pool; goes to no one."""
        return self.potential_mojos - self.reward_mojos


@dataclass(frozen=True)
class DailyRewards:
    model_version: str
    ceiling_mojos: int
    total_weight: Fraction
    rewards: tuple[TreeReward, ...]
    ineligible: tuple[TreeDay, ...] = field(default=())
    no_eligible_trees: bool = False

    @property
    def distributed_mojos(self) -> int:
        return sum(r.reward_mojos for r in self.rewards)

    @property
    def unearned_mojos(self) -> int:
        """Available but not earned. The runway extension."""
        return self.ceiling_mojos - self.distributed_mojos

    def by_wallet(self) -> dict[str, int]:
        """Per-wallet totals — the settlement view.

        Summed only at the end: a wallet's total is the sum of what its Trees
        each earned. It is never an input to the calculation, which is what
        makes wallet-splitting and wallet-merging both pointless.
        """
        out: dict[str, int] = {}
        for r in self.rewards:
            if r.reward_mojos:
                out[r.wallet_address] = out.get(r.wallet_address, 0) + r.reward_mojos
        return out


def calculate_daily_rewards(trees: list[TreeDay], ceiling_mojos: int) -> DailyRewards:
    """Split a day's network ceiling between eligible Trees.

    Guarantees, each pinned by tests:
      * the total never exceeds ``ceiling_mojos``
      * a Tree's downtime is forfeited to the pool, never to another Tree
      * more Trees never raise the total
      * more sensors never raise the total
      * the result is identical for any input ordering
      * no binary float is used anywhere on the path from inputs to mojos
    """
    if not isinstance(ceiling_mojos, int) or isinstance(ceiling_mojos, bool):
        raise RewardError("ceiling_mojos must be an int (mojos are indivisible)")
    if ceiling_mojos < 0:
        raise RewardError(f"ceiling_mojos cannot be negative: {ceiling_mojos}")

    ineligible = tuple(t for t in trees if not t.eligible)
    eligible = [t for t in trees if t.eligible]

    # A Tree with no qualifying sensor earns nothing and — just as importantly —
    # contributes nothing to total_weight. Counting it in the denominator would
    # let a heartbeat-only board dilute every real Tree's share while earning
    # zero itself, which is a cheaper attack than participating honestly.
    weighted = [(t, sensor_weight(t.qualifying_sensors)) for t in eligible]
    weighted = [(t, w) for t, w in weighted if w > 0]

    if not weighted:
        return DailyRewards(
            model_version=MODEL_VERSION, ceiling_mojos=ceiling_mojos,
            total_weight=Fraction(0), rewards=(), ineligible=ineligible,
            no_eligible_trees=True)

    total_weight = sum((w for _, w in weighted), Fraction(0))

    rewards = []
    for tree, weight in sorted(weighted, key=lambda tw: (tw[0].tree_id,
                                                         tw[0].wallet_address)):
        share = weight / total_weight
        potential = Fraction(ceiling_mojos) * share
        earned = potential * tree.uptime_factor
        rewards.append(TreeReward(
            tree_id=tree.tree_id,
            wallet_address=tree.wallet_address,
            sensor_weight=weight,
            verified_heartbeats=tree.verified_heartbeats,
            uptime_factor=tree.uptime_factor,
            potential_mojos=potential.numerator // potential.denominator,
            reward_mojos=earned.numerator // earned.denominator,
            share=share,
        ))

    result = DailyRewards(
        model_version=MODEL_VERSION, ceiling_mojos=ceiling_mojos,
        total_weight=total_weight, rewards=tuple(rewards), ineligible=ineligible)

    if result.distributed_mojos > ceiling_mojos:
        # Unreachable: every reward is floored and the shares sum to 1. Raising
        # rather than returning keeps an arithmetic bug from reaching a wallet.
        raise RewardError(
            f"distributed {result.distributed_mojos} exceeds ceiling "
            f"{ceiling_mojos} — bug in the reward calculation")
    return result
