# SPDX-License-Identifier: Apache-2.0
"""Transaction planner — the last component that can say no.

Everything upstream describes the world; everything downstream acts on it. The
planner is where a description becomes an intention, so every rule that could
prevent a bad spend is enforced here rather than in the executor. The executor
should be boring: take instructions, send them, record what happened.

A refusal is a first-class outcome, not an exception. ``plan()`` always returns
a ``SpendPlan``; a plan that must not run carries ``blocked_by`` and the
executor will not touch it. That way the reason is auditable and printable
rather than a stack trace in a log nobody reads.

THE RULES, AND WHAT EACH ONE IS ACTUALLY PROTECTING AGAINST
===========================================================

  pause switch          A human needs a way to stop the machine that does not
                        involve editing code or racing a timer. A file on disk
                        works when the config system, the network, and your
                        memory of the flag names do not.

  in-flight             An instruction stuck in `sending` means a previous run
                        may or may not have moved money. Continuing would risk
                        paying twice, and no automatic guess is safe.

  cycle already settled Re-running a completed cycle is the ordinary way to
                        double-pay: the operator runs it by hand, forgets, and
                        the timer fires.

  max per cycle         Bounds the blast radius of a bad budget, a bad config
                        edit, or a decimal in the wrong place.

  max per wallet        Bounds the blast radius of one wallet dominating the
                        weight — including a sybil that beat the averaging.

  dust floor            A spend smaller than its own fee costs more than it
                        pays. It is dropped, not rounded up.

  balance               Checked against the SUM, before anything is sent, so a
                        run cannot pay the first three wallets and then discover
                        it cannot pay the fourth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import audit as audit_mod
from .engine import AllocationResult


@dataclass(frozen=True)
class SpendInstruction:
    """One unsigned intention. Carries no key, no signature, no coin."""
    wallet_address: str
    amount_mojos: int
    idempotency_key: str
    memo: str
    wallet_avg_uptime: str
    pair_count: int


@dataclass(frozen=True)
class SpendPlan:
    cycle_id: str
    asset_id: str
    period_start: datetime
    period_end: datetime
    budget_mojos: int
    instructions: tuple[SpendInstruction, ...]
    dropped: tuple[tuple[str, int, str], ...] = ()   # (wallet, mojos, why)
    blocked_by: tuple[str, ...] = ()
    fee_mojos: int = 0
    dry_run: bool = True
    warnings: tuple[str, ...] = field(default=())

    @property
    def total_mojos(self) -> int:
        return sum(i.amount_mojos for i in self.instructions)

    @property
    def runnable(self) -> bool:
        return not self.blocked_by and bool(self.instructions)


@dataclass(frozen=True)
class PlannerLimits:
    """All safety bounds in one place, so a review is one object wide."""
    max_per_cycle_mojos: int
    max_per_wallet_mojos: int
    min_payout_mojos: int = 1          # dust floor
    fee_mojos: int = 0
    pause_file: Path | None = None

    def paused(self) -> bool:
        return bool(self.pause_file and self.pause_file.exists())


def plan(
    result: AllocationResult,
    *,
    store: audit_mod.AuditStore,
    asset_id: str,
    period_start: datetime,
    period_end: datetime,
    limits: PlannerLimits,
    available_balance_mojos: int | None,
    uptime_basis: str,
    dry_run: bool = True,
) -> SpendPlan:
    """Turn allocations into instructions, or explain why not."""
    cycle_id = audit_mod.cycle_id_for(
        period_start=period_start, period_end=period_end,
        budget_mojos=result.budget_mojos, asset_id=asset_id)

    blocked: list[str] = []
    warnings: list[str] = []
    dropped: list[tuple[str, int, str]] = []

    if limits.paused():
        blocked.append(
            f"PAUSED — {limits.pause_file} exists. Delete it to resume. "
            f"Nothing was planned or sent.")

    stuck = store.in_flight()
    if stuck:
        who = ", ".join(f"{s.wallet_address[:14]}…({s.amount_mojos} mojos, "
                        f"cycle {s.cycle_id[:8]})" for s in stuck[:5])
        blocked.append(
            f"{len(stuck)} instruction(s) left mid-send: {who}. It is not known "
            f"whether those moved funds. Resolve them by hand — check the wallet "
            f"for the transaction, then mark the row sent or failed — before any "
            f"further cycle runs.")

    existing = store.get_cycle(cycle_id)
    if existing is not None:
        sent = store.total_sent_mojos(cycle_id)
        if sent > 0:
            blocked.append(
                f"cycle {cycle_id[:12]} has already sent {sent} mojos. The same "
                f"period and budget is the same cycle by definition; re-running "
                f"it would pay again.")
        else:
            warnings.append(
                f"cycle {cycle_id[:12]} was planned before but sent nothing — "
                f"re-planning it.")

    # Build instructions from the allocation, applying per-wallet rules.
    instructions: list[SpendInstruction] = []
    for a in result.allocations:
        amount = a.amount_mojos
        if amount <= 0:
            dropped.append((a.wallet_address, amount, "zero allocation"))
            continue
        if amount < limits.min_payout_mojos:
            dropped.append((a.wallet_address, amount,
                            f"below the {limits.min_payout_mojos}-mojo dust floor"))
            continue
        if amount > limits.max_per_wallet_mojos:
            # Capped, not dropped — the wallet earned something, just not this
            # much. The shortfall is deliberately NOT redistributed: that would
            # let a cap on one wallet silently inflate another.
            dropped.append((a.wallet_address, amount - limits.max_per_wallet_mojos,
                            f"capped at the {limits.max_per_wallet_mojos}-mojo "
                            f"per-wallet maximum"))
            amount = limits.max_per_wallet_mojos

        instructions.append(SpendInstruction(
            wallet_address=a.wallet_address,
            amount_mojos=amount,
            idempotency_key=audit_mod.instruction_key(cycle_id, a.wallet_address),
            memo=f"orchard:{cycle_id[:12]}",
            wallet_avg_uptime=str(a.average_uptime_bp),
            pair_count=a.pair_count,
        ))

    total = sum(i.amount_mojos for i in instructions)

    if total > limits.max_per_cycle_mojos:
        blocked.append(
            f"plan totals {total} mojos, over the {limits.max_per_cycle_mojos} "
            f"per-cycle maximum. Nothing is sent. Either the budget or the cap "
            f"is wrong, and the machine must not decide which.")

    need = total + (limits.fee_mojos * len(instructions))
    if available_balance_mojos is not None and need > available_balance_mojos:
        blocked.append(
            f"insufficient balance: the plan needs {need} mojos "
            f"({total} + fees) and the wallet holds {available_balance_mojos}. "
            f"Refusing to send a partial payout — some wallets paid and others "
            f"not is harder to reason about than nothing having happened.")

    if result.unspent_reason:
        warnings.append(f"budget not fully allocated: {result.unspent_reason}")
    elif result.remainder_mojos:
        warnings.append(f"{result.remainder_mojos} mojos unallocated")

    return SpendPlan(
        cycle_id=cycle_id, asset_id=asset_id,
        period_start=period_start, period_end=period_end,
        budget_mojos=result.budget_mojos, instructions=tuple(instructions),
        dropped=tuple(dropped), blocked_by=tuple(blocked),
        fee_mojos=limits.fee_mojos, dry_run=dry_run, warnings=tuple(warnings),
    )


def persist(plan_: SpendPlan, result: AllocationResult, records,
            store: audit_mod.AuditStore, uptime_basis: str) -> None:
    """Write the plan and everything that produced it, before anything is sent."""
    store.open_cycle(
        cycle_id=plan_.cycle_id, period_start=plan_.period_start,
        period_end=plan_.period_end, budget_mojos=plan_.budget_mojos,
        allocated_mojos=result.allocated_mojos,
        total_weight=str(result.total_weight), asset_id=plan_.asset_id,
        uptime_basis=uptime_basis, dry_run=plan_.dry_run,
        notes="; ".join(plan_.warnings))
    store.record_inputs(plan_.cycle_id, records)
    for i in plan_.instructions:
        store.put_instruction(
            cycle_id=plan_.cycle_id, wallet_address=i.wallet_address,
            amount_mojos=i.amount_mojos, wallet_avg_uptime=i.wallet_avg_uptime,
            pair_count=i.pair_count)
    store.event("planned", cycle_id=plan_.cycle_id,
                instructions=len(plan_.instructions), total=plan_.total_mojos,
                blocked=list(plan_.blocked_by), dry_run=plan_.dry_run)
