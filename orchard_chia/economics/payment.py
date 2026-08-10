# SPDX-License-Identifier: Apache-2.0
"""Turn settled days into spendable plans — through the existing safety stack.

The pool ledger says what each wallet is owed for a settled day. This module
hands that to the allocation service's planner and executor, which were kept
precisely because their safety properties are model-independent:

  * dry-run by default, two deliberate acts to go live
  * cycle identity derived from WHAT is being paid, so re-running a crashed
    payment resumes it instead of paying twice
  * an instruction stuck in `sending` blocks every later cycle
  * an unknown spend outcome halts everything and waits for a human

One settled day = one payment cycle. The cycle's identity is the season's
bounds plus the day's distributed total, so the same day can never become two
different cycles, and the audit store's UNIQUE idempotency key is the last
line against paying it twice.

Payment marks the ledger (paid_at) only after the executor reports every
instruction sent. A day paid in part stays unpaid in the ledger and the audit
store's per-instruction state carries the detail — the ledger never says
"done" about money that may not have moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..allocation import audit as audit_mod
from ..allocation.engine import UptimeRecord
from ..allocation.planner import PlannerLimits, SpendPlan, persist, plan
from .constants import HEARTBEATS_PER_DAY
from .ledger import PoolLedger


class PaymentError(RuntimeError):
    pass


def _season_bounds_utc(day_index: int, genesis: datetime) -> tuple[datetime, datetime]:
    start = genesis + timedelta(days=day_index)
    return start, start + timedelta(days=1)


@dataclass(frozen=True)
class DayPayment:
    day_index: int
    plan: SpendPlan
    total_mojos: int


def ensure_paid_column(ledger: PoolLedger) -> None:
    """Additive column recording when a day's payment fully left the wallet."""
    cols = {r[1] for r in ledger._c.execute(
        "PRAGMA table_info(settled_days)").fetchall()}
    if "paid_at" not in cols:
        ledger._c.execute(
            "ALTER TABLE settled_days ADD COLUMN paid_at TEXT")
        ledger._c.commit()


def unpaid_days(ledger: PoolLedger) -> list[int]:
    ensure_paid_column(ledger)
    rows = ledger._c.execute(
        "SELECT day_index FROM settled_days "
        "WHERE paid_at IS NULL AND distributed_mojos > 0 ORDER BY day_index"
    ).fetchall()
    return [int(r["day_index"]) for r in rows]


def mark_paid(ledger: PoolLedger, day_index: int) -> None:
    ensure_paid_column(ledger)
    ledger._c.execute(
        "UPDATE settled_days SET paid_at=? WHERE day_index=?",
        (datetime.now(timezone.utc).isoformat(), day_index))
    ledger._c.commit()


def plan_day_payment(
    ledger: PoolLedger,
    day_index: int,
    *,
    store: audit_mod.AuditStore,
    asset_id: str,
    genesis: datetime,
    limits: PlannerLimits,
    available_balance_mojos: int | None,
    dry_run: bool = True,
) -> DayPayment:
    """One settled day -> one spend plan, through every planner rule.

    The rewards are read back from the ledger's own per-Tree rows — the same
    rows an auditor would read — never recomputed from the oracle, which may
    by now say something different about a day that is already settled.
    """
    ensure_paid_column(ledger)
    day = ledger.day(day_index)
    if day is None:
        raise PaymentError(f"day {day_index} is not settled; settle it first")
    if day["paid_at"]:
        raise PaymentError(
            f"day {day_index} was already paid at {day['paid_at']}. The audit "
            f"store has the per-wallet detail; re-planning it would be the "
            f"double-payment this whole stack exists to prevent.")

    rows = ledger._c.execute(
        "SELECT tree_id, wallet_address, heartbeats, reward_mojos "
        "FROM day_rewards WHERE day_index=? AND reward_mojos > 0",
        (day_index,)).fetchall()
    if not rows:
        raise PaymentError(f"day {day_index} distributed nothing; nothing to pay")

    # Rebuild through the allocation engine's record type so the planner's
    # audit trail carries the same shape for every payment it has ever made.
    # uptime_bp here is heartbeats/24 in basis points — informational in the
    # audit row; the AMOUNT comes from the ledger, not from re-derivation.
    from ..allocation.engine import AllocationResult, WalletAllocation
    from fractions import Fraction

    per_wallet: dict[str, int] = {}
    pairs: dict[str, int] = {}
    for r in rows:
        per_wallet[r["wallet_address"]] = (
            per_wallet.get(r["wallet_address"], 0) + int(r["reward_mojos"]))
        pairs[r["wallet_address"]] = pairs.get(r["wallet_address"], 0) + 1

    total = sum(per_wallet.values())
    allocations = tuple(
        WalletAllocation(
            wallet_address=w, amount_mojos=m,
            average_uptime_bp=Fraction(0),      # not an input; see docstring
            pair_count=pairs[w],
            share=Fraction(m, total) if total else Fraction(0),
            pairs=(),
        )
        for w, m in sorted(per_wallet.items()))
    result = AllocationResult(
        budget_mojos=total, allocated_mojos=total,
        allocations=allocations, total_weight=Fraction(1))

    start, end = _season_bounds_utc(day_index, genesis)
    the_plan = plan(
        result, store=store, asset_id=asset_id,
        period_start=start, period_end=end, limits=limits,
        available_balance_mojos=available_balance_mojos,
        uptime_basis="economics-ledger", dry_run=dry_run)
    records = [UptimeRecord(
        tree_id=r["tree_id"], sensor_id="ledger",
        wallet_address=r["wallet_address"],
        uptime_bp=min(10_000, int(r["heartbeats"]) * 10_000 // HEARTBEATS_PER_DAY),
    ) for r in rows]
    persist(the_plan, result, records, store, "economics-ledger")
    return DayPayment(day_index=day_index, plan=the_plan, total_mojos=total)
