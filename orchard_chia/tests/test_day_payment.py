# SPDX-License-Identifier: Apache-2.0
"""A settled day becomes a spend plan through the full safety stack.

The amounts come from the ledger's own per-Tree rows — what an auditor reads —
never recomputed from an oracle that may by now say something different about
a day already settled. One day is one cycle, identified by what it pays, so a
crashed payment resumes and a completed one refuses to repeat.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchard_chia.allocation import audit as audit_mod
from orchard_chia.allocation.executor import execute
from orchard_chia.allocation.planner import PlannerLimits
from orchard_chia.economics import PoolLedger, TreeDay, settle_day
from orchard_chia.economics.payment import (PaymentError, mark_paid,
                                            plan_day_payment, unpaid_days)

W1 = "xch1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
W2 = "xch1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ASSET = "285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121"
GENESIS = datetime(2026, 5, 27, tzinfo=timezone.utc)


class FakeSpender:
    def __init__(self):
        self.sends = []

    def spendable_balance(self):
        return 10**12

    def send(self, ins):
        self.sends.append((ins.wallet_address, ins.amount_mojos))
        return f"0xtx{len(self.sends):04d}"

    def confirmed(self, tx):
        return True


@pytest.fixture()
def world(tmp_path):
    ledger = PoolLedger(tmp_path / "pool.db")
    store = audit_mod.AuditStore(tmp_path / "audit.db")
    trees = [
        TreeDay(tree_id="T1", wallet_address=W1, qualifying_sensors=1,
                verified_heartbeats=24),
        TreeDay(tree_id="T2", wallet_address=W2, qualifying_sensors=1,
                verified_heartbeats=12),
    ]
    s = settle_day(trees, day_index=0, pool_remaining_mojos=10_000_000)
    ledger.record(s)
    yield ledger, store, s
    ledger.close(); store.close()


def limits():
    return PlannerLimits(max_per_cycle_mojos=10**12, max_per_wallet_mojos=10**12)


def test_amounts_come_from_the_ledger_rows(world):
    ledger, store, s = world
    p = plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=10**12)
    got = {i.wallet_address: i.amount_mojos for i in p.plan.instructions}
    assert got == s.payable_by_wallet
    assert p.total_mojos == s.distributed_mojos


def test_an_unsettled_day_cannot_be_paid(world):
    ledger, store, _ = world
    with pytest.raises(PaymentError, match="not settled"):
        plan_day_payment(ledger, 7, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=None)


def test_a_paid_day_refuses_to_plan_again(world):
    ledger, store, _ = world
    mark_paid(ledger, 0)
    with pytest.raises(PaymentError, match="already paid"):
        plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=None)


def test_unpaid_days_lists_only_what_owes(world):
    ledger, _, _ = world
    assert unpaid_days(ledger) == [0]
    mark_paid(ledger, 0)
    assert unpaid_days(ledger) == []


def test_the_same_day_is_the_same_cycle(world):
    """Re-planning maps to the identical cycle_id, so the audit store's
    already-paid guard is live before any second spend."""
    ledger, store, _ = world
    p1 = plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                          genesis=GENESIS, limits=limits(),
                          available_balance_mojos=None)
    p2 = plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                          genesis=GENESIS, limits=limits(),
                          available_balance_mojos=None)
    assert p1.plan.cycle_id == p2.plan.cycle_id


def test_end_to_end_a_settled_day_reaches_the_wallet_once(world):
    ledger, store, s = world
    p = plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=10**12, dry_run=False)
    spender = FakeSpender()
    report = execute(p.plan, store=store, spender=spender)
    assert report.ok and sorted(spender.sends) == sorted(s.payable_by_wallet.items())
    mark_paid(ledger, 0)

    # A second full pass cannot move money again anywhere in the stack.
    with pytest.raises(PaymentError):
        plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=10**12, dry_run=False)


def test_dry_run_is_the_default_and_spends_nothing(world):
    ledger, store, _ = world
    p = plan_day_payment(ledger, 0, store=store, asset_id=ASSET,
                         genesis=GENESIS, limits=limits(),
                         available_balance_mojos=10**12)
    spender = FakeSpender()
    report = execute(p.plan, store=store, spender=spender)
    assert report.dry_run and spender.sends == []
