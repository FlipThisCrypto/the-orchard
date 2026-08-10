# SPDX-License-Identifier: Apache-2.0
"""The allocation engine. Pure, deterministic, and the only place the money
arithmetic happens.

No I/O, no clock, no randomness, no network, no config lookups, no logging.
Same inputs give the same output on any machine, in any order, forever. That
matters more here than anywhere else in the system: this function decides what
people are paid, and its output is about to be signed.

TWO DECISIONS WORTH READING BEFORE CHANGING ANYTHING
====================================================

1. Exact arithmetic, not floats.

   Uptime arrives as a percentage and budgets are integers, so the obvious
   implementation is ``budget * pct / total``. Binary floating point cannot
   represent 0.1, so that implementation produces sums that miss the budget by
   a mojo or two, and — worse — misses it differently depending on the order
   wallets happen to arrive in. Allocations are computed with
   ``fractions.Fraction`` throughout and only become integers at the very last
   step. There is no rounding until there is exactly one place for it.

2. Largest-remainder, with a deterministic tie-break.

   Dividing an integer budget by rational weights leaves a remainder that must
   go somewhere. Rounding each wallet independently does not sum to the budget;
   it can be over or under by up to one mojo per wallet. So: floor every
   allocation, then hand the leftover mojos out one at a time to the wallets
   with the largest discarded fraction — the Hamilton method.

   Ties are broken by wallet address, ascending. Not by input order, which
   would make the result depend on how a database happened to sort a query, and
   not by anything random. Two wallets with identical claims resolve the same
   way every run, and a re-run of a cycle produces a byte-identical plan.

THE ZERO CASE
=============

If every eligible wallet has 0% uptime, the total weight is zero and there is
no meaningful proportion to divide by. The engine does NOT divide by zero, does
not fall back to an equal split, and does not spend the budget. Every wallet
gets 0 and the budget goes unspent. "0 hours uptime is 0 hours uptime meaning
0% payout" — the owner's rule, and the only defensible reading: a budget is a
ceiling on what may be paid, never an amount that must be.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

# Uptime is carried as an integer count of BASIS POINTS of a percent:
# 10_000 bp == 100.00%. Integers because uptime originates as a count of hours
# out of a count of hours, and because an integer cannot pick up the drift a
# float does when it is summed across a fleet.
BASIS_POINTS_FULL = 10_000


@dataclass(frozen=True)
class UptimeRecord:
    """One eligible (tree, sensor) pair and its measured uptime for a cycle.

    Frozen because the engine must not be able to alter its own inputs; if an
    allocation is ever disputed, the records that produced it are exactly the
    records that were read.
    """
    tree_id: str
    sensor_id: str
    wallet_address: str
    uptime_bp: int                  # 0 .. 10_000
    eligible: bool = True
    # Why an ineligible record was excluded. Carried rather than dropped so the
    # audit trail can show what was considered and rejected, not merely what won.
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.tree_id or not self.sensor_id:
            raise ValueError("tree_id and sensor_id are required")
        if not self.wallet_address:
            raise ValueError(f"{self.tree_id}/{self.sensor_id} has no wallet_address")
        if not isinstance(self.uptime_bp, int) or isinstance(self.uptime_bp, bool):
            raise ValueError(
                f"uptime_bp must be an int, got {type(self.uptime_bp).__name__} "
                f"({self.uptime_bp!r}). Floats are refused so that a rounding "
                f"decision cannot be made accidentally, upstream, and invisibly."
            )
        if not (0 <= self.uptime_bp <= BASIS_POINTS_FULL):
            raise ValueError(
                f"uptime_bp {self.uptime_bp} out of range for "
                f"{self.tree_id}/{self.sensor_id} (0..{BASIS_POINTS_FULL})"
            )


@dataclass(frozen=True)
class WalletWeight:
    """A wallet's averaged claim on the budget, before any money is involved."""
    wallet_address: str
    pair_count: int
    total_uptime_bp: int
    average_uptime_bp: Fraction      # exact; total / count
    pairs: tuple[tuple[str, str], ...]   # (tree_id, sensor_id), sorted


@dataclass(frozen=True)
class WalletAllocation:
    wallet_address: str
    amount_mojos: int
    average_uptime_bp: Fraction
    pair_count: int
    share: Fraction                  # exact proportion of total weight
    pairs: tuple[tuple[str, str], ...]

    @property
    def average_uptime_percent(self) -> float:
        """For display only. Never feed this back into the arithmetic."""
        return float(self.average_uptime_bp) / 100.0


@dataclass(frozen=True)
class AllocationResult:
    budget_mojos: int
    allocated_mojos: int
    allocations: tuple[WalletAllocation, ...]
    total_weight: Fraction
    excluded: tuple[UptimeRecord, ...] = field(default=())
    # Set when the budget was deliberately not spent, with the reason.
    unspent_reason: str | None = None

    @property
    def remainder_mojos(self) -> int:
        return self.budget_mojos - self.allocated_mojos

    def by_wallet(self) -> dict[str, int]:
        return {a.wallet_address: a.amount_mojos for a in self.allocations}


def wallet_weights(records: list[UptimeRecord]) -> list[WalletWeight]:
    """Group eligible records by wallet and average their uptime.

    The average — not the sum — is what a wallet is weighted by. See the module
    docstring in ``__init__.py`` for why, and what it costs.
    """
    grouped: dict[str, list[UptimeRecord]] = {}
    for r in records:
        if not r.eligible:
            continue
        grouped.setdefault(r.wallet_address, []).append(r)

    out: list[WalletWeight] = []
    for addr in sorted(grouped):
        rs = grouped[addr]
        total = sum(r.uptime_bp for r in rs)
        out.append(WalletWeight(
            wallet_address=addr,
            pair_count=len(rs),
            total_uptime_bp=total,
            average_uptime_bp=Fraction(total, len(rs)),
            pairs=tuple(sorted((r.tree_id, r.sensor_id) for r in rs)),
        ))
    return out


def allocate(records: list[UptimeRecord], budget_mojos: int) -> AllocationResult:
    """Split ``budget_mojos`` between wallets in proportion to average uptime.

    Guarantees, all of which are tested:
      * the returned allocations sum to EXACTLY ``budget_mojos``, unless the
        total weight is zero, in which case they sum to exactly 0
      * no allocation is negative
      * the output is identical for any input ordering
      * no float is used anywhere on the path from uptime to mojos
    """
    if not isinstance(budget_mojos, int) or isinstance(budget_mojos, bool):
        raise ValueError(
            f"budget_mojos must be an int (mojos are indivisible), got "
            f"{type(budget_mojos).__name__}"
        )
    if budget_mojos < 0:
        raise ValueError(f"budget_mojos must not be negative, got {budget_mojos}")

    excluded = tuple(r for r in records if not r.eligible)
    weights = wallet_weights(records)

    if not weights:
        return AllocationResult(
            budget_mojos=budget_mojos, allocated_mojos=0, allocations=(),
            total_weight=Fraction(0), excluded=excluded,
            unspent_reason="no eligible tree/sensor pairs",
        )

    total_weight = sum((w.average_uptime_bp for w in weights), Fraction(0))

    if total_weight == 0:
        # Every eligible wallet measured zero uptime. Nobody has a claim, so
        # nobody is paid and the budget is not spent. Deliberately NOT an equal
        # split: an equal split would pay for downtime.
        return AllocationResult(
            budget_mojos=budget_mojos, allocated_mojos=0,
            allocations=tuple(WalletAllocation(
                wallet_address=w.wallet_address, amount_mojos=0,
                average_uptime_bp=w.average_uptime_bp, pair_count=w.pair_count,
                share=Fraction(0), pairs=w.pairs,
            ) for w in weights),
            total_weight=Fraction(0), excluded=excluded,
            unspent_reason="every eligible wallet measured 0% uptime",
        )

    # Exact share, then floor, then hand back the remainder. Nothing rounds
    # until the floor, and the floor happens exactly once.
    exact: list[tuple[WalletWeight, Fraction, int, Fraction]] = []
    for w in weights:
        share = w.average_uptime_bp / total_weight
        precise = budget_mojos * share
        floored = precise.numerator // precise.denominator
        exact.append((w, share, floored, precise - floored))

    allocated = sum(f for _, _, f, _ in exact)
    leftover = budget_mojos - allocated

    # Largest discarded fraction first; ties by address ascending so the result
    # never depends on input order or on how a query happened to sort.
    order = sorted(range(len(exact)),
                   key=lambda i: (-exact[i][3], exact[i][0].wallet_address))
    bump = {i: 0 for i in range(len(exact))}
    for n in range(leftover):
        bump[order[n % len(order)]] += 1

    allocations = tuple(
        WalletAllocation(
            wallet_address=w.wallet_address,
            amount_mojos=floored + bump[i],
            average_uptime_bp=w.average_uptime_bp,
            pair_count=w.pair_count,
            share=share,
            pairs=w.pairs,
        )
        for i, (w, share, floored, _frac) in enumerate(exact)
    )

    total = sum(a.amount_mojos for a in allocations)
    if total != budget_mojos:
        # Unreachable by construction. Raising rather than returning keeps a
        # arithmetic bug from ever reaching the planner, let alone a signature.
        raise AssertionError(
            f"allocation does not sum to budget: {total} != {budget_mojos}. "
            f"This is a bug in the engine; nothing downstream should proceed."
        )

    return AllocationResult(
        budget_mojos=budget_mojos, allocated_mojos=total,
        allocations=allocations, total_weight=total_weight, excluded=excluded,
    )
