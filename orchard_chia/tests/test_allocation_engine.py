# SPDX-License-Identifier: Apache-2.0
"""The allocation engine — the arithmetic that decides what people are paid.

Every test here is about one of three properties, because those are the three
ways this can go wrong in a way nobody notices until it has been signed:

  * it sums to the budget EXACTLY, always, including when the division is ugly
  * it is deterministic — same inputs, same output, regardless of ordering
  * zero uptime pays zero, and never quietly becomes an equal split

The fixtures follow the owner's specification, including the worked example,
which is reproduced verbatim as ``test_the_specified_worked_example``.
"""
from __future__ import annotations

import random
from fractions import Fraction

import pytest

from orchard_chia.allocation.engine import (
    BASIS_POINTS_FULL, UptimeRecord, allocate, wallet_weights,
)

A = "xch1walletaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "xch1walletbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "xch1walletcccccccccccccccccccccccccccccccccccccccccccccccccccccc"


def rec(wallet, tree, sensor, pct, eligible=True, reason=None):
    """Build a record from a human percentage. 100 -> 10_000 bp."""
    return UptimeRecord(
        tree_id=tree, sensor_id=sensor, wallet_address=wallet,
        uptime_bp=int(round(pct * 100)), eligible=eligible,
        exclusion_reason=reason,
    )


# --- the specification's own example ---------------------------------------

def test_the_specified_worked_example():
    """Wallet A 100/80 -> 90, B 90/70 -> 80, C 30 -> 30. Weight 200.
    A 1000 budget must split 450 / 400 / 150."""
    result = allocate([
        rec(A, "t1", "s1", 100), rec(A, "t2", "s2", 80),
        rec(B, "t3", "s3", 90),  rec(B, "t4", "s4", 70),
        rec(C, "t5", "s5", 30),
    ], budget_mojos=1000)

    assert result.by_wallet() == {A: 450, B: 400, C: 150}
    assert result.allocated_mojos == 1000
    # 200% expressed in basis points
    assert result.total_weight == Fraction(20000)


# --- the required fixtures --------------------------------------------------

def test_one_wallet_one_sensor_takes_the_whole_budget():
    r = allocate([rec(A, "t1", "s1", 42)], budget_mojos=1000)
    assert r.by_wallet() == {A: 1000}, "a sole eligible wallet receives all of it"


def test_one_wallet_several_sensors_still_takes_the_whole_budget():
    """The count of sensors changes nothing when there is nobody to share with."""
    r = allocate([rec(A, "t1", "s1", 100), rec(A, "t2", "s2", 50),
                  rec(A, "t3", "s3", 25)], budget_mojos=999)
    assert r.by_wallet() == {A: 999}


def test_several_wallets_one_sensor_each():
    r = allocate([rec(A, "t1", "s1", 50), rec(B, "t2", "s2", 30),
                  rec(C, "t3", "s3", 20)], budget_mojos=1000)
    assert r.by_wallet() == {A: 500, B: 300, C: 200}


def test_several_wallets_several_sensors():
    r = allocate([
        rec(A, "t1", "s1", 100), rec(A, "t2", "s2", 100),   # avg 100
        rec(B, "t3", "s3", 60),  rec(B, "t4", "s4", 40),    # avg 50
        rec(C, "t5", "s5", 50),  rec(C, "t6", "s6", 50),    # avg 50
    ], budget_mojos=2000)
    assert r.by_wallet() == {A: 1000, B: 500, C: 500}


def test_more_sensors_does_not_buy_a_bigger_share():
    """The defining property of averaging: ten perfect Trees weigh the same as
    one. If this ever fails, the model has silently become sum-weighted and the
    sybil resistance is gone."""
    one = allocate([rec(A, "t1", "s1", 100), rec(B, "t2", "s2", 100)],
                   budget_mojos=1000)
    many = allocate([rec(A, "t1", "s1", 100)]
                    + [rec(B, f"t{i}", f"s{i}", 100) for i in range(2, 12)],
                    budget_mojos=1000)
    assert one.by_wallet() == {A: 500, B: 500}
    assert many.by_wallet() == {A: 500, B: 500}


def test_adding_a_bad_sensor_dilutes_that_wallet():
    """The cost of the same property, pinned so it stays a decision."""
    before = allocate([rec(A, "t1", "s1", 100), rec(B, "t2", "s2", 100)],
                      budget_mojos=1000)
    after = allocate([rec(A, "t1", "s1", 100), rec(A, "t9", "s9", 0),
                      rec(B, "t2", "s2", 100)], budget_mojos=1000)
    assert before.by_wallet()[A] == 500
    assert after.by_wallet()[A] < before.by_wallet()[A], (
        "averaging means a dead Tree costs its owner money"
    )
    assert after.by_wallet() == {A: 333, B: 667}


def test_full_half_and_zero_uptime():
    r = allocate([rec(A, "t1", "s1", 100), rec(B, "t2", "s2", 50),
                  rec(C, "t3", "s3", 0)], budget_mojos=1500)
    assert r.by_wallet() == {A: 1000, B: 500, C: 0}
    assert r.allocated_mojos == 1500


def test_zero_uptime_everywhere_pays_nothing_and_spends_nothing():
    """The owner's rule: 0 hours uptime is 0% payout. Not an equal split."""
    r = allocate([rec(A, "t1", "s1", 0), rec(B, "t2", "s2", 0)],
                 budget_mojos=1000)
    assert r.by_wallet() == {A: 0, B: 0}
    assert r.allocated_mojos == 0
    assert r.remainder_mojos == 1000, "the budget is a ceiling, not a quota"
    assert "0%" in (r.unspent_reason or "")


def test_no_eligible_pairs_at_all():
    r = allocate([rec(A, "t1", "s1", 100, eligible=False, reason="stale")],
                 budget_mojos=1000)
    assert r.allocations == ()
    assert r.allocated_mojos == 0
    assert r.excluded and r.excluded[0].exclusion_reason == "stale"


def test_ineligible_pairs_are_reported_not_silently_dropped():
    r = allocate([rec(A, "t1", "s1", 100),
                  rec(A, "t2", "s2", 100, eligible=False, reason="sensor stale"),
                  rec(B, "t3", "s3", 100)], budget_mojos=1000)
    assert r.by_wallet() == {A: 500, B: 500}, "the stale pair must not raise A's average"
    assert [e.exclusion_reason for e in r.excluded] == ["sensor stale"]


def test_a_duplicate_sensor_row_is_the_callers_problem_but_is_visible():
    """The engine does not de-duplicate — that is the collector's job, and
    hiding it here would hide a real data fault. It DOES have to stay
    arithmetically sound when it happens."""
    r = allocate([rec(A, "t1", "s1", 100), rec(A, "t1", "s1", 0),
                  rec(B, "t2", "s2", 50)], budget_mojos=1000)
    weights = {w.wallet_address: w for w in wallet_weights([
        rec(A, "t1", "s1", 100), rec(A, "t1", "s1", 0), rec(B, "t2", "s2", 50)])}
    assert weights[A].pair_count == 2, "both rows counted; dedupe belongs upstream"
    assert weights[A].average_uptime_bp == Fraction(5000)
    assert sum(a.amount_mojos for a in r.allocations) == 1000


# --- rounding ---------------------------------------------------------------

def test_rounding_remainder_is_distributed_and_the_sum_is_exact():
    """1000 split three equal ways is 333.33... — the classic case where naive
    rounding loses or invents a mojo."""
    r = allocate([rec(A, "t1", "s1", 50), rec(B, "t2", "s2", 50),
                  rec(C, "t3", "s3", 50)], budget_mojos=1000)
    assert sum(r.by_wallet().values()) == 1000
    assert sorted(r.by_wallet().values()) == [333, 333, 334]


def test_the_extra_mojo_goes_to_a_deterministic_wallet_not_a_random_one():
    got = {tuple(sorted(allocate(
        [rec(A, "t1", "s1", 50), rec(B, "t2", "s2", 50), rec(C, "t3", "s3", 50)],
        budget_mojos=1000).by_wallet().items())) for _ in range(25)}
    assert len(got) == 1, "the same cycle must produce the same plan every time"


def test_ties_break_by_address_ascending():
    """Three identical claims, one spare mojo. It goes to the lowest address."""
    r = allocate([rec(A, "t1", "s1", 50), rec(B, "t2", "s2", 50),
                  rec(C, "t3", "s3", 50)], budget_mojos=1000)
    assert r.by_wallet()[A] == 334, "lowest address wins an exact tie"
    assert r.by_wallet()[B] == 333 and r.by_wallet()[C] == 333


@pytest.mark.parametrize("budget", [1, 2, 7, 99, 1000, 1001, 999_999, 10**9 + 7])
def test_the_sum_is_exact_for_awkward_budgets(budget):
    r = allocate([rec(A, "t1", "s1", 37), rec(B, "t2", "s2", 63),
                  rec(C, "t3", "s3", 11), rec(C, "t4", "s4", 89)],
                 budget_mojos=budget)
    assert sum(r.by_wallet().values()) == budget
    assert all(v >= 0 for v in r.by_wallet().values())


def test_a_budget_smaller_than_the_wallet_count_still_balances():
    """2 mojos, 5 wallets. Three wallets must get nothing, and that is correct."""
    r = allocate([rec(a, f"t{i}", f"s{i}", 50) for i, a in
                  enumerate([A, B, C, "xch1d" + "d" * 58, "xch1e" + "e" * 58])],
                 budget_mojos=2)
    assert sum(r.by_wallet().values()) == 2
    assert sorted(r.by_wallet().values()) == [0, 0, 0, 1, 1]


def test_a_zero_budget_allocates_zero():
    r = allocate([rec(A, "t1", "s1", 100)], budget_mojos=0)
    assert r.by_wallet() == {A: 0}


# --- determinism ------------------------------------------------------------

def test_input_order_cannot_change_the_outcome():
    records = [rec(A, "t1", "s1", 100), rec(A, "t2", "s2", 33),
               rec(B, "t3", "s3", 71),  rec(C, "t4", "s4", 12),
               rec(C, "t5", "s5", 88),  rec(C, "t6", "s6", 5)]
    baseline = allocate(records, budget_mojos=123_457).by_wallet()
    rng = random.Random(20260810)
    for _ in range(50):
        shuffled = records[:]
        rng.shuffle(shuffled)
        assert allocate(shuffled, budget_mojos=123_457).by_wallet() == baseline


def test_no_float_ever_touches_the_arithmetic():
    """A float share would make the result host-dependent at the last digit."""
    r = allocate([rec(A, "t1", "s1", 100), rec(B, "t2", "s2", 33)],
                 budget_mojos=10**12)
    for a in r.allocations:
        assert isinstance(a.share, Fraction)
        assert isinstance(a.average_uptime_bp, Fraction)
        assert isinstance(a.amount_mojos, int)


# --- refusals ---------------------------------------------------------------

def test_a_float_uptime_is_refused_at_the_boundary():
    with pytest.raises(ValueError, match="must be an int"):
        UptimeRecord(tree_id="t", sensor_id="s", wallet_address=A, uptime_bp=50.5)


def test_uptime_above_100_percent_is_refused():
    with pytest.raises(ValueError, match="out of range"):
        UptimeRecord(tree_id="t", sensor_id="s", wallet_address=A,
                     uptime_bp=BASIS_POINTS_FULL + 1)


def test_a_negative_budget_is_refused():
    with pytest.raises(ValueError, match="negative"):
        allocate([rec(A, "t1", "s1", 100)], budget_mojos=-1)


def test_a_fractional_budget_is_refused():
    with pytest.raises(ValueError, match="must be an int"):
        allocate([rec(A, "t1", "s1", 100)], budget_mojos=100.5)


def test_a_pair_with_no_wallet_is_refused_rather_than_dropped():
    """Silently dropping it would shrink the denominator and quietly pay
    everyone else more."""
    with pytest.raises(ValueError, match="no wallet_address"):
        UptimeRecord(tree_id="t", sensor_id="s", wallet_address="", uptime_bp=100)
