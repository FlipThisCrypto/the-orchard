# SPDX-License-Identifier: Apache-2.0
"""The pool's memory. Append-only, derived balance, invariant enforced at write.

The balance is never stored — it is always 85,000,000 minus the sum of settled
days, so falsifying it requires falsifying the rows it is computed from. A day
settles once; a different second answer for the same day is refused loudly,
because that is the one situation in which silence would launder a bug.
"""
from __future__ import annotations

import pytest

from orchard_chia.economics import (TREE_REWARDS_POOL_MOJOS, LedgerError,
                                    PoolLedger, TreeDay, settle_day)

W = "xch1wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww"


def day(ledger, idx, beats=24, n=1):
    trees = [TreeDay(tree_id=f"T{i}", wallet_address=W, qualifying_sensors=1,
                     verified_heartbeats=beats) for i in range(n)]
    snap = ledger.snapshot()
    return settle_day(trees, day_index=idx,
                      pool_remaining_mojos=snap.remaining_mojos)


@pytest.fixture()
def ledger(tmp_path):
    with PoolLedger(tmp_path / "pool.db") as led:
        yield led


def test_a_fresh_ledger_holds_the_whole_pool(ledger):
    snap = ledger.snapshot()
    assert snap.remaining_mojos == TREE_REWARDS_POOL_MOJOS
    assert snap.days_settled == 0 and snap.last_day_index is None


def test_the_balance_is_derived_from_settled_days(ledger):
    s = day(ledger, 0)
    ledger.record(s)
    snap = ledger.snapshot()
    assert snap.distributed_total_mojos == s.distributed_mojos
    assert snap.remaining_mojos == TREE_REWARDS_POOL_MOJOS - s.distributed_mojos
    assert snap.last_day_index == 0


def test_settling_across_a_restart_uses_the_recorded_balance(tmp_path):
    """The whole reason the ledger exists."""
    path = tmp_path / "pool.db"
    with PoolLedger(path) as led:
        led.record(day(led, 0))
        before = led.snapshot().remaining_mojos
    with PoolLedger(path) as led:          # a new process
        assert led.snapshot().remaining_mojos == before
        led.record(day(led, 1))
        assert led.snapshot().days_settled == 2


def test_a_day_settles_exactly_once(ledger):
    s = day(ledger, 0)
    ledger.record(s)
    with pytest.raises(LedgerError, match="already settled with the same total"):
        ledger.record(s)


def test_a_different_answer_for_the_same_day_is_a_loud_error(ledger):
    ledger.record(day(ledger, 0, beats=24))
    with pytest.raises(LedgerError, match="DIFFERENT total"):
        ledger.record(day(ledger, 0, beats=12))


def test_settling_backwards_is_refused(ledger):
    ledger.record(day(ledger, 5))
    with pytest.raises(LedgerError, match="precedes already-settled"):
        ledger.record(day(ledger, 3))


def test_gaps_forward_are_allowed(ledger):
    """A network that was down for a week settles day 7 after day 0 — the
    missed days simply emitted nothing."""
    ledger.record(day(ledger, 0))
    ledger.record(day(ledger, 7))
    assert ledger.snapshot().days_settled == 2


def test_the_fixed_supply_invariant_is_enforced_at_write(ledger):
    """Even if every upstream check were broken, the ledger refuses. The forged
    settlement here could not come out of settle_day — that is the point: the
    ledger is the last line, and its refusal survives a bug in everything
    upstream of it."""
    class Forged:
        day_index = 0
        distributed_mojos = TREE_REWARDS_POOL_MOJOS + 1
        unearned_mojos = 0
        pool_closing_mojos = 0
        class ceiling:
            ceiling_mojos = TREE_REWARDS_POOL_MOJOS + 1
        class rewards:
            rewards = ()

    with pytest.raises(LedgerError, match="past the .* pool"):
        ledger.record(Forged())


def test_per_tree_rewards_are_recorded_for_audit(ledger):
    ledger.record(day(ledger, 0, n=3))
    rows = ledger._c.execute("SELECT * FROM day_rewards WHERE day_index=0").fetchall()
    assert len(rows) == 3
    assert all(r["wallet_address"] == W for r in rows)


def test_a_corrupted_ledger_refuses_everything(ledger):
    """A sum past the pool in the DATA means the invariant is already broken;
    every operation stops until a human looks."""
    ledger._c.execute(
        "INSERT INTO settled_days VALUES (0,'t','2.0.0',1,?,0,1,0)",
        (TREE_REWARDS_POOL_MOJOS + 5,))
    ledger._c.commit()
    with pytest.raises(LedgerError, match="invariant is broken in the data"):
        ledger.snapshot()


def test_the_basis_of_every_reward_is_recorded(ledger):
    """An auditor a year from now must be able to tell a chain-verified
    reward from an oracle-trusted one by reading the ledger alone."""
    import dataclasses
    trees = [TreeDay(tree_id="T1", wallet_address=W, qualifying_sensors=1,
                     verified_heartbeats=24, heartbeat_basis="chain:verified_hours")]
    s = settle_day(trees, day_index=0,
                   pool_remaining_mojos=ledger.snapshot().remaining_mojos)
    ledger.record(s)
    row = ledger._c.execute(
        "SELECT basis FROM day_rewards WHERE day_index=0").fetchone()
    assert row["basis"] == "chain:verified_hours"


def test_an_old_ledger_gains_the_basis_column(tmp_path):
    """Ledgers created before the column open cleanly and read as
    oracle-hours — what those rows in fact were."""
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE settled_days (day_index INTEGER PRIMARY KEY,
            settled_at TEXT NOT NULL, model_version TEXT NOT NULL,
            ceiling_mojos INTEGER NOT NULL, distributed_mojos INTEGER NOT NULL,
            unearned_mojos INTEGER NOT NULL, eligible_trees INTEGER NOT NULL,
            pool_closing_mojos INTEGER NOT NULL);
        CREATE TABLE day_rewards (day_index INTEGER NOT NULL,
            tree_id TEXT NOT NULL, wallet_address TEXT NOT NULL,
            sensor_weight TEXT NOT NULL, heartbeats INTEGER NOT NULL,
            reward_mojos INTEGER NOT NULL, PRIMARY KEY (day_index, tree_id));
        INSERT INTO day_rewards VALUES (0,'T1','xch1w','1',24,1000);
    """)
    c.commit(); c.close()
    with PoolLedger(path) as led:
        row = led._c.execute(
            "SELECT basis FROM day_rewards WHERE day_index=0").fetchone()
        assert row["basis"] == "oracle-hours"


def test_a_clean_ledger_audits_clean(ledger):
    ledger.record(day(ledger, 0))
    ledger.record(day(ledger, 1, beats=12))
    assert ledger.audit() == []


def test_audit_catches_a_tampered_day_total(ledger):
    ledger.record(day(ledger, 0))
    ledger._c.execute(
        "UPDATE settled_days SET distributed_mojos = distributed_mojos + 5 "
        "WHERE day_index=0")
    ledger._c.commit()
    problems = ledger.audit()
    assert any("per-Tree rows sum" in p for p in problems)


def test_audit_catches_a_broken_pool_chain(ledger):
    ledger.record(day(ledger, 0))
    ledger.record(day(ledger, 1))
    ledger._c.execute(
        "UPDATE settled_days SET pool_closing_mojos = pool_closing_mojos - 1 "
        "WHERE day_index=1")
    ledger._c.commit()
    problems = ledger.audit()
    assert any("closing balance" in p for p in problems)


def test_audit_catches_distribution_over_ceiling(ledger):
    ledger.record(day(ledger, 0))
    ledger._c.execute(
        "UPDATE settled_days SET ceiling_mojos = 1 WHERE day_index=0")
    ledger._c.commit()
    assert any("exceeds its recorded ceiling" in p for p in ledger.audit())
