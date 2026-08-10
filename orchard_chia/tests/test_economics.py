# SPDX-License-Identifier: Apache-2.0
"""The $JUICE emission model, pinned.

Three kinds of test here, and the middle one matters most:

  * the formula produces the specified numbers
  * the ECONOMIC INVARIANTS hold — fixed supply, a ceiling more Trees cannot
    raise, unearned rewards staying in the pool, no redistribution of forfeited
    rewards. These are the promises made to operators, and code drifts.
  * the arithmetic is exact and order-independent, so an independent
    implementation given the same inputs computes the same mojos.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from orchard_chia import economics as ec
from orchard_chia.economics import (DAILY_EMISSION_MOJOS_BY_YEAR,
                                    HEARTBEATS_PER_DAY, MOJOS_PER_JUICE,
                                    TREE_REWARDS_POOL_MOJOS, EmissionError,
                                    RewardError, TreeDay,
                                    apply_distribution, calculate_daily_rewards,
                                    daily_ceiling_mojos, emission_year_for_day,
                                    schedule_total_mojos, scheduled_daily_mojos,
                                    sensor_weight)

W_A = "xch1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
W_B = "xch1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
W_C = "xch1cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

YEAR1 = DAILY_EMISSION_MOJOS_BY_YEAR[1]


def tree(tid, wallet=W_A, sensors=1, beats=HEARTBEATS_PER_DAY, **kw):
    return TreeDay(tree_id=tid, wallet_address=wallet,
                   qualifying_sensors=sensors, verified_heartbeats=beats, **kw)


# --- supply and allocation --------------------------------------------------

def test_the_supply_splits_85_15():
    assert ec.TOTAL_SUPPLY_MOJOS == 100_000_000 * MOJOS_PER_JUICE
    assert ec.TREE_REWARDS_POOL_MOJOS == 85_000_000 * MOJOS_PER_JUICE
    assert ec.LIQUIDITY_MOJOS == 15_000_000 * MOJOS_PER_JUICE
    assert ec.TREE_REWARDS_POOL_MOJOS + ec.LIQUIDITY_MOJOS == ec.TOTAL_SUPPLY_MOJOS


def test_there_is_no_founder_allocation():
    from orchard_chia.economics.constants import FOUNDER_ALLOCATION_MOJOS
    assert FOUNDER_ALLOCATION_MOJOS == 0


# --- the eight-year schedule ------------------------------------------------

def test_each_year_is_about_twenty_percent_below_the_last():
    for y in range(2, 9):
        prev, cur = scheduled_daily_mojos(y - 1), scheduled_daily_mojos(y)
        ratio = Fraction(cur, prev)
        assert Fraction(799, 1000) < ratio < Fraction(801, 1000), (
            f"year {y} is {float(ratio):.4f} of year {y - 1}")


def test_the_schedule_spends_the_pool_over_eight_years():
    """What makes eight years the intended runway: if every scheduled reward
    were earned in full, the pool would be spent almost exactly."""
    total = schedule_total_mojos()
    drift = abs(total - TREE_REWARDS_POOL_MOJOS)
    assert drift < TREE_REWARDS_POOL_MOJOS // 1_000_000, (
        f"schedule totals {ec.format_juice(total)} against a pool of "
        f"{ec.format_juice(TREE_REWARDS_POOL_MOJOS)}")


@pytest.mark.parametrize("year,expected_juice", [
    (1, "55964.65"), (2, "44771.72"), (3, "35817.38"), (4, "28653.90"),
    (5, "22923.12"), (6, "18338.50"), (7, "14670.80"), (8, "11736.64"),
])
def test_each_year_matches_the_published_rate(year, expected_juice):
    assert scheduled_daily_mojos(year) == int(round(float(expected_juice) * 1000))


def test_year_boundaries_fall_on_day_365():
    assert emission_year_for_day(0) == 1
    assert emission_year_for_day(364) == 1
    assert emission_year_for_day(365) == 2
    assert emission_year_for_day(729) == 2
    assert emission_year_for_day(730) == 3


@pytest.mark.parametrize("day,year", [(0, 1), (365, 2), (1095, 4), (2555, 8)])
def test_year_transitions_change_the_rate(day, year):
    c = daily_ceiling_mojos(day, TREE_REWARDS_POOL_MOJOS)
    assert c.year == year
    assert c.scheduled_mojos == DAILY_EMISSION_MOJOS_BY_YEAR[year]


def test_after_year_eight_the_terminal_rate_continues():
    """The schedule is a floor on the programme's life, not an expiry."""
    c = daily_ceiling_mojos(365 * 9, 10_000_000_000)
    assert c.year == 10 and c.past_schedule
    assert c.scheduled_mojos == DAILY_EMISSION_MOJOS_BY_YEAR[8]


def test_a_day_before_genesis_is_refused():
    with pytest.raises(EmissionError, match="precedes network genesis"):
        emission_year_for_day(-1)


# --- the specified worked examples ------------------------------------------

def test_a_single_tree_takes_the_whole_ceiling():
    """The One-Tree Rule, stated in the spec and deliberate."""
    r = calculate_daily_rewards([tree("T1")], YEAR1)
    assert r.distributed_mojos == YEAR1
    assert r.unearned_mojos == 0


def test_a_single_tree_at_half_uptime_earns_half_and_forfeits_half():
    r = calculate_daily_rewards([tree("T1", beats=12)], YEAR1)
    assert r.distributed_mojos == YEAR1 // 2
    assert r.unearned_mojos == YEAR1 - YEAR1 // 2


def test_the_sensor_weighting_example_from_the_spec():
    """1,000 JUICE, three Trees at weights 1.00 / 1.10 / 1.25, the last at 12/24."""
    pool = 1_000 * MOJOS_PER_JUICE
    r = calculate_daily_rewards([
        tree("A", W_A, sensors=1, beats=24),
        tree("B", W_B, sensors=3, beats=24),
        tree("C", W_C, sensors=6, beats=12),
    ], pool)

    assert r.total_weight == Fraction(67, 20)          # 3.35
    got = {x.tree_id: x for x in r.rewards}
    assert got["A"].potential_mojos == pool * 20 // 67
    assert got["B"].potential_mojos == pool * 22 // 67
    assert got["C"].potential_mojos == pool * 25 // 67
    assert got["C"].reward_mojos == got["C"].potential_mojos // 2

    # ~813.43 distributed, ~186.57 left. The spec's 813.44 comes from rounding
    # each Tree to two decimals first; exact mojo arithmetic lands just below.
    assert 813_000 <= r.distributed_mojos <= 813_500
    assert r.distributed_mojos + r.unearned_mojos == pool


def test_ten_equal_trees_split_the_pool_evenly():
    pool = 1_000 * MOJOS_PER_JUICE
    r = calculate_daily_rewards([tree(f"T{i}", W_A) for i in range(10)], pool)
    assert {x.reward_mojos for x in r.rewards} == {100 * MOJOS_PER_JUICE}


# --- the invariants ---------------------------------------------------------

def test_more_trees_never_raise_total_emission():
    """Invariant 4. The early-adopter incentive is exactly this and nothing else."""
    for n in (1, 2, 10, 100, 1000):
        r = calculate_daily_rewards([tree(f"T{i}", W_A) for i in range(n)], YEAR1)
        assert r.distributed_mojos <= YEAR1


def test_more_sensors_never_raise_total_emission():
    """Invariant 6. Weighting redistributes; it cannot mint."""
    plain = calculate_daily_rewards(
        [tree(f"T{i}", W_A, sensors=1) for i in range(5)], YEAR1)
    loaded = calculate_daily_rewards(
        [tree(f"T{i}", W_A, sensors=6) for i in range(5)], YEAR1)
    assert loaded.distributed_mojos <= YEAR1
    assert plain.distributed_mojos <= YEAR1


def test_a_missed_heartbeat_is_forfeited_not_handed_to_another_tree():
    """Invariant 9, and the one an operator would notice first if it broke."""
    full = calculate_daily_rewards([tree("A", W_A), tree("B", W_B)], YEAR1)
    half = calculate_daily_rewards(
        [tree("A", W_A), tree("B", W_B, beats=12)], YEAR1)

    a_full = next(x for x in full.rewards if x.tree_id == "A").reward_mojos
    a_half = next(x for x in half.rewards if x.tree_id == "A").reward_mojos
    assert a_half == a_full, "A must not gain from B's downtime"
    assert half.unearned_mojos > 0


def test_unearned_juice_stays_in_the_pool():
    """Invariant 5 — the rule that turns eight years into a minimum."""
    pool = TREE_REWARDS_POOL_MOJOS
    ceiling = daily_ceiling_mojos(0, pool)
    r = calculate_daily_rewards([tree("T1", beats=18)], ceiling.ceiling_mojos)
    state = apply_distribution(pool, ceiling, r.distributed_mojos)

    assert state.unearned_mojos == ceiling.ceiling_mojos - r.distributed_mojos
    assert state.closing_mojos == pool - r.distributed_mojos
    assert state.closing_mojos > pool - ceiling.ceiling_mojos, (
        "the unearned quarter must still be in the pool")


def test_wallet_count_cannot_change_network_emission():
    """Invariant 7. Splitting Trees across wallets, or merging them, changes
    nothing — which is what makes the wallet layer not worth gaming."""
    one_wallet = calculate_daily_rewards(
        [tree("T1", W_A), tree("T2", W_A), tree("T3", W_A)], YEAR1)
    three_wallets = calculate_daily_rewards(
        [tree("T1", W_A), tree("T2", W_B), tree("T3", W_C)], YEAR1)
    assert one_wallet.distributed_mojos == three_wallets.distributed_mojos


def test_a_wallet_receives_the_sum_of_its_trees():
    r = calculate_daily_rewards(
        [tree("T1", W_A), tree("T2", W_A), tree("T3", W_A), tree("T4", W_B)],
        YEAR1)
    by_wallet = r.by_wallet()
    assert len(by_wallet) == 2
    assert by_wallet[W_A] == sum(
        x.reward_mojos for x in r.rewards if x.wallet_address == W_A)
    assert by_wallet[W_A] > by_wallet[W_B], "three Trees earn more than one"


def test_the_total_never_exceeds_the_ceiling_for_awkward_tree_counts():
    for n in (3, 7, 11, 23, 97, 383):
        r = calculate_daily_rewards([tree(f"T{i}", W_A) for i in range(n)], YEAR1)
        assert r.distributed_mojos <= YEAR1


def test_rewards_stop_when_the_pool_is_empty():
    """Invariant 10."""
    c = daily_ceiling_mojos(0, 0)
    assert c.ceiling_mojos == 0 and c.limited_by_pool
    r = calculate_daily_rewards([tree("T1")], c.ceiling_mojos)
    assert r.distributed_mojos == 0


def test_a_pool_smaller_than_the_daily_rate_becomes_the_ceiling():
    remaining = 1_234_567
    c = daily_ceiling_mojos(0, remaining)
    assert c.ceiling_mojos == remaining and c.limited_by_pool
    r = calculate_daily_rewards([tree("T1")], c.ceiling_mojos)
    assert r.distributed_mojos == remaining
    assert apply_distribution(remaining, c, r.distributed_mojos).closing_mojos == 0


def test_the_pool_can_never_go_negative():
    c = daily_ceiling_mojos(0, 1000)
    with pytest.raises(EmissionError, match="exceeds today's ceiling"):
        apply_distribution(1000, c, 1001)


def test_overspending_is_refused_rather_than_trimmed():
    """Clamping would pay a number no rule produced, and hide the bug."""
    c = daily_ceiling_mojos(0, TREE_REWARDS_POOL_MOJOS)
    with pytest.raises(EmissionError, match="Refusing to trim"):
        apply_distribution(TREE_REWARDS_POOL_MOJOS, c, c.ceiling_mojos + 1)


def test_cumulative_distribution_cannot_exceed_the_pool():
    """Invariants 1 and 2."""
    from orchard_chia.economics import pool_after
    assert pool_after(TREE_REWARDS_POOL_MOJOS) == 0
    with pytest.raises(EmissionError, match="fixed-supply invariant"):
        pool_after(TREE_REWARDS_POOL_MOJOS + 1)


# --- sensor weighting -------------------------------------------------------

@pytest.mark.parametrize("sensors,expected", [
    (1, Fraction(100, 100)), (2, Fraction(105, 100)), (3, Fraction(110, 100)),
    (4, Fraction(115, 100)), (5, Fraction(120, 100)), (6, Fraction(125, 100)),
])
def test_the_sensor_weight_table(sensors, expected):
    assert sensor_weight(sensors) == expected


def test_the_bonus_is_capped_at_twenty_five_percent():
    for n in (6, 7, 20, 500):
        assert sensor_weight(n) == Fraction(125, 100)


def test_a_tree_with_no_qualifying_sensor_earns_nothing():
    """An ESP32 that only sends heartbeats is not environmental infrastructure."""
    r = calculate_daily_rewards(
        [tree("REAL", W_A, sensors=1), tree("BARE", W_B, sensors=0)], YEAR1)
    got = {x.tree_id for x in r.rewards}
    assert got == {"REAL"}
    assert r.distributed_mojos == YEAR1, "and it must not dilute the real Tree"


def test_a_sensorless_tree_does_not_dilute_the_denominator():
    """Counting it in total_weight would let a heartbeat-only board shrink
    every real Tree's share while earning nothing — cheaper than participating."""
    alone = calculate_daily_rewards([tree("REAL", W_A)], YEAR1)
    with_bare = calculate_daily_rewards(
        [tree("REAL", W_A), tree("BARE", W_B, sensors=0)], YEAR1)
    assert alone.distributed_mojos == with_bare.distributed_mojos


def test_negative_sensor_counts_are_refused():
    with pytest.raises(RewardError, match="negative"):
        sensor_weight(-1)


# --- uptime -----------------------------------------------------------------

@pytest.mark.parametrize("beats,numer", [(24, 1), (18, 3), (12, 1), (6, 1), (0, 0)])
def test_uptime_factors(beats, numer):
    t = tree("T", beats=beats)
    assert t.uptime_factor.numerator == numer


def test_zero_uptime_earns_zero():
    r = calculate_daily_rewards([tree("T1", beats=0)], YEAR1)
    assert r.distributed_mojos == 0
    assert r.unearned_mojos == YEAR1


def test_more_than_twenty_four_heartbeats_is_refused():
    """A burst of heartbeats must not buy more than a day of uptime."""
    with pytest.raises(RewardError, match="outside 0..24"):
        tree("T", beats=25)


def test_negative_heartbeats_are_refused():
    with pytest.raises(RewardError, match="outside 0..24"):
        tree("T", beats=-1)


# --- eligibility ------------------------------------------------------------

def test_no_eligible_trees_distributes_nothing():
    r = calculate_daily_rewards([], YEAR1)
    assert r.no_eligible_trees and r.distributed_mojos == 0
    assert r.unearned_mojos == YEAR1


def test_an_ineligible_tree_is_recorded_not_silently_dropped():
    r = calculate_daily_rewards([
        tree("GOOD", W_A),
        TreeDay(tree_id="BAD", wallet_address=W_B, qualifying_sensors=3,
                verified_heartbeats=24, eligible=False,
                ineligible_reason="duplicate device key"),
    ], YEAR1)
    assert [x.tree_id for x in r.rewards] == ["GOOD"]
    assert r.ineligible[0].ineligible_reason == "duplicate device key"
    assert r.distributed_mojos == YEAR1, "an excluded Tree must not dilute"


def test_an_eligible_tree_without_a_wallet_is_refused():
    with pytest.raises(RewardError, match="no wallet to pay"):
        TreeDay(tree_id="T", wallet_address="", qualifying_sensors=1,
                verified_heartbeats=24)


# --- exactness and determinism ----------------------------------------------

def test_no_binary_float_touches_the_arithmetic():
    r = calculate_daily_rewards(
        [tree("A", W_A, sensors=3), tree("B", W_B, sensors=1)], YEAR1)
    for x in r.rewards:
        assert isinstance(x.share, Fraction)
        assert isinstance(x.sensor_weight, Fraction)
        assert isinstance(x.uptime_factor, Fraction)
        assert isinstance(x.reward_mojos, int)


def test_input_order_cannot_change_any_reward():
    import random
    trees = [tree(f"T{i}", [W_A, W_B, W_C][i % 3], sensors=1 + i % 6,
                  beats=(i * 7) % 25) for i in range(31)]
    baseline = {x.tree_id: x.reward_mojos
                for x in calculate_daily_rewards(trees, YEAR1).rewards}
    rng = random.Random(20260810)
    for _ in range(25):
        shuffled = trees[:]
        rng.shuffle(shuffled)
        got = {x.tree_id: x.reward_mojos
               for x in calculate_daily_rewards(shuffled, YEAR1).rewards}
        assert got == baseline


def test_rounding_always_favours_the_pool():
    """Three Trees splitting an indivisible amount: the remainder must stay in
    the pool, never be handed to a Tree to make the sum come out even."""
    r = calculate_daily_rewards(
        [tree("A", W_A), tree("B", W_B), tree("C", W_C)], 100)
    assert [x.reward_mojos for x in r.rewards] == [33, 33, 33]
    assert r.unearned_mojos == 1


def test_a_ceiling_of_zero_pays_nothing():
    r = calculate_daily_rewards([tree("T1")], 0)
    assert r.distributed_mojos == 0


def test_a_negative_ceiling_is_refused():
    with pytest.raises(RewardError, match="negative"):
        calculate_daily_rewards([tree("T1")], -1)


def test_the_model_version_travels_with_the_result():
    """An amount computed under one model and audited under another is
    unfalsifiable."""
    r = calculate_daily_rewards([tree("T1")], YEAR1)
    assert r.model_version == ec.MODEL_VERSION


# --- a full multi-year simulation -------------------------------------------

def test_a_year_at_partial_uptime_extends_the_runway():
    """The headline claim, simulated rather than asserted: a network that does
    not earn everything keeps going longer than eight years."""
    pool = TREE_REWARDS_POOL_MOJOS
    trees = [tree(f"T{i}", W_A, sensors=2, beats=18) for i in range(4)]  # 75%

    day = 0
    while not ec.is_exhausted(pool, len(trees)) and day < 365 * 40:
        ceiling = daily_ceiling_mojos(day, pool)
        r = calculate_daily_rewards(trees, ceiling.ceiling_mojos)
        pool = apply_distribution(pool, ceiling, r.distributed_mojos).closing_mojos
        day += 1

    # Two bounds here were wrong before the model was: 12 years left 8.4M JUICE
    # unspent, and looping on `pool > 0` never terminated at all — at 75% uptime
    # the pool strands 4 mojos that only full uptime can clear. Forfeiting a
    # quarter of every day is supposed to compound, and it does.
    assert day > 365 * 8, (
        f"at 75% uptime the programme lasted {day / 365:.1f} years — it must "
        f"outlive the eight-year schedule, or unearned rewards are leaking")
    assert day > 365 * 13, (
        f"only {day / 365:.1f} years — a quarter of every day going unearned "
        f"should extend the runway substantially, not marginally")
    assert pool < 100, f"{pool} mojos left — expected only rounding dust"


def test_a_stalled_pool_is_not_an_exhausted_one():
    """The dust at the end is still earnable, and a scheduler must not write it
    off. Four Trees at 75% distribute nothing from 4 mojos; the same four at
    100% clear it exactly."""
    stalled = calculate_daily_rewards(
        [tree(f"T{i}", W_A, beats=18) for i in range(4)], 4)
    assert stalled.distributed_mojos == 0
    assert not ec.is_exhausted(4, 4), "paying nothing today is not the end"

    cleared = calculate_daily_rewards(
        [tree(f"T{i}", W_A, beats=24) for i in range(4)], 4)
    assert cleared.distributed_mojos == 4, "perfect uptime still clears it"


def test_exhaustion_is_when_no_uptime_could_earn_a_mojo():
    assert ec.is_exhausted(0, 4)
    assert ec.is_exhausted(3, 4), "3 mojos cannot pay 4 Trees a mojo each"
    assert not ec.is_exhausted(4, 4)
    assert not ec.is_exhausted(1, 1)


def test_full_uptime_lands_close_to_eight_years():
    pool = TREE_REWARDS_POOL_MOJOS
    trees = [tree("T1", W_A)]
    day = 0
    while pool > 0 and day < 365 * 12:
        ceiling = daily_ceiling_mojos(day, pool)
        r = calculate_daily_rewards(trees, ceiling.ceiling_mojos)
        pool = apply_distribution(pool, ceiling, r.distributed_mojos).closing_mojos
        day += 1
    assert 365 * 8 - 5 <= day <= 365 * 8 + 5, f"{day} days"
