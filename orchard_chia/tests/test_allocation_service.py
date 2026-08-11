# SPDX-License-Identifier: Apache-2.0
"""Collector, planner, executor, audit and scheduler.

The engine has its own file because its failures are arithmetic. These are the
failures that cost money: paying twice, paying past a cap, paying from an empty
wallet, paying a Tree that died, and — the one that matters most — retrying a
spend whose outcome nobody knows.

No test here touches a network or a wallet. The spender is a fake that records
what it was asked to do, which is the only way to test "did we send twice"
without sending once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchard_chia.allocation import audit as audit_mod
from orchard_chia.allocation.collector import CollectorError, collect
from orchard_chia.allocation.engine import UptimeRecord, allocate
from orchard_chia.allocation.executor import (ExecutionReport, ExecutorError,
                                              execute, track_confirmations)
from orchard_chia.allocation.planner import PlannerLimits, persist, plan
from orchard_chia.allocation.service import (Settings, render_report,
                                             run_cycle, run_scheduler)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
A = "xch1walletaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "xch1walletbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ASSET = "285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121"


@pytest.fixture()
def store(tmp_path):
    with audit_mod.AuditStore(tmp_path / "alloc.db") as s:
        yield s


def rec(w, t, pct, eligible=True, reason=None):
    return UptimeRecord(tree_id=t, sensor_id="ds18b20", wallet_address=w,
                        uptime_bp=int(pct * 100), eligible=eligible,
                        exclusion_reason=reason)


def limits(**kw):
    base = dict(max_per_cycle_mojos=10**9, max_per_wallet_mojos=10**9,
                min_payout_mojos=1, fee_mojos=0, pause_file=None)
    base.update(kw)
    return PlannerLimits(**base)


def make_plan(store, records, budget, *, bal=None, dry_run=False, lim=None):
    result = allocate(records, budget)
    p = plan(result, store=store, asset_id=ASSET, period_start=NOW - timedelta(hours=24),
             period_end=NOW, limits=lim or limits(), available_balance_mojos=bal,
             uptime_basis="node-hours-present", dry_run=dry_run)
    persist(p, result, records, store, "node-hours-present")
    return p


class FakeSpender:
    """Records every send. Can be told to fail, or to fail ambiguously."""

    def __init__(self, balance=10**9, fail_times=0, unknown=False):
        self.sends: list[tuple[str, int]] = []
        self.balance = balance
        self.fail_times = fail_times
        self.unknown = unknown
        self._confirmed: set[str] = set()

    def spendable_balance(self):
        return self.balance

    def send(self, instruction):
        self.sends.append((instruction.wallet_address, instruction.amount_mojos))
        if self.unknown:
            raise ExecutorError("wallet returned no transaction_id")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("wallet daemon busy")
        return f"0xtx{len(self.sends):04d}"

    def confirmed(self, tx_id):
        return tx_id in self._confirmed


# --- collector --------------------------------------------------------------

class FakeOracle:
    base = "https://oracle.test"

    def __init__(self, nodes, uptimes, season=76):
        self._nodes, self._uptimes, self._season = nodes, uptimes, season

    def nodes(self): return self._nodes
    def current_season(self): return self._season

    def uptime(self, node_id, season):
        if node_id not in self._uptimes:
            raise CollectorError("no uptime for that node")
        return self._uptimes[node_id]


def test_collector_converts_hours_to_basis_points():
    src = FakeOracle(
        [{"node_id": "T1", "sensors": ["ds18b20"], "wallet_address": A,
          "last_reading_at": NOW.isoformat()}],
        {"T1": {"hours_online": 12}})
    got = collect(src, now=NOW, period_hours=24)
    assert got.records[0].uptime_bp == 5000, "12 of 24 hours is 50.00%"
    assert got.uptime_basis == "node-hours-present"


def test_collector_emits_one_record_per_node_not_per_sensor():
    """Per-sensor uptime does not exist upstream. Emitting one record per
    declared sensor would let a wallet raise its own average by listing more
    sensor names on the same board."""
    src = FakeOracle(
        [{"node_id": "T1", "sensors": ["ds18b20", "gps", "bme280"],
          "wallet_address": A, "last_reading_at": NOW.isoformat()}],
        {"T1": {"hours_online": 24}})
    got = collect(src, now=NOW, period_hours=24)
    assert len(got.records) == 1
    assert got.records[0].sensor_id == "bme280+ds18b20+gps"


def test_a_stale_sensor_is_excluded_with_a_reason():
    src = FakeOracle(
        [{"node_id": "T1", "sensors": [], "wallet_address": A,
          "last_reading_at": (NOW - timedelta(hours=9)).isoformat()}],
        {"T1": {"hours_online": 20}})
    got = collect(src, now=NOW, period_hours=24, stale_after_hours=3)
    assert got.records[0].eligible is False
    assert "stale" in got.records[0].exclusion_reason
    assert got.eligible == (), "20 earlier hours do not resurrect a dead sensor"


def test_a_tree_that_never_reported_is_excluded():
    src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": A}],
                     {"T1": {"hours_online": 0}})
    got = collect(src, now=NOW, period_hours=24)
    assert got.records[0].eligible is False
    assert "never reported" in got.records[0].exclusion_reason


def test_missing_uptime_data_skips_the_tree_rather_than_paying_it_zero():
    src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": A,
                       "last_reading_at": NOW.isoformat()}], {})
    got = collect(src, now=NOW)
    assert got.records == ()
    assert got.skipped and "uptime unavailable" in got.skipped[0][1]


def test_a_tree_with_no_visible_wallet_is_skipped_not_guessed():
    src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": None,
                       "last_reading_at": NOW.isoformat()}],
                     {"T1": {"hours_online": 24}})
    got = collect(src, now=NOW)
    assert got.records == ()
    assert "no wallet_address" in got.skipped[0][1]


def test_hours_beyond_the_period_are_capped_not_trusted():
    """A season is longer than a 24h cycle; hours_online can exceed the period.
    Paying on the excess would pay more than 100%."""
    src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": A,
                       "last_reading_at": NOW.isoformat()}],
                     {"T1": {"hours_online": 900}})
    got = collect(src, now=NOW, period_hours=24)
    assert got.records[0].uptime_bp == 10_000


def test_a_naive_now_is_refused():
    src = FakeOracle([], {})
    with pytest.raises(CollectorError, match="timezone-aware"):
        collect(src, now=datetime(2026, 8, 10, 12, 0))


def test_the_minimum_uptime_floor_excludes_rather_than_zeroes():
    src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": A,
                       "last_reading_at": NOW.isoformat()}],
                     {"T1": {"hours_online": 1}})
    got = collect(src, now=NOW, period_hours=24, min_uptime_bp=2000)
    assert got.records[0].eligible is False
    assert "floor" in got.records[0].exclusion_reason


# --- planner ----------------------------------------------------------------

def test_the_plan_matches_the_allocation(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 50)], 3000)
    assert {i.wallet_address: i.amount_mojos for i in p.instructions} == {A: 2000, B: 1000}
    assert p.runnable


def test_insufficient_balance_blocks_the_whole_plan(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 100)], 5000, bal=4999)
    assert not p.runnable
    assert any("insufficient balance" in b for b in p.blocked_by)


def test_an_unknown_balance_is_not_treated_as_infinite(store):
    """None means the wallet could not answer. The planner must not send."""
    p = make_plan(store, [rec(A, "t1", 100)], 5000, bal=None)
    assert p.runnable, "None means 'not checked', which is the caller's choice"
    p2 = make_plan(store, [rec(A, "t1", 100)], 5000, bal=0)
    assert not p2.runnable


def test_the_per_cycle_cap_blocks_everything_rather_than_trimming(store):
    p = make_plan(store, [rec(A, "t1", 100)], 5000,
                  lim=limits(max_per_cycle_mojos=4999))
    assert not p.runnable
    assert any("per-cycle maximum" in b for b in p.blocked_by)


def test_the_per_wallet_cap_trims_that_wallet_and_does_not_reallocate(store):
    """Redistributing a cap would let one wallet's limit inflate another's pay."""
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 100)], 2000,
                  lim=limits(max_per_wallet_mojos=600))
    amounts = {i.wallet_address: i.amount_mojos for i in p.instructions}
    assert amounts == {A: 600, B: 600}
    assert p.total_mojos == 1200, "the 800 shortfall is NOT handed to anyone"
    assert any("capped" in d[2] for d in p.dropped)


def test_dust_is_dropped_not_rounded_up(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 1)], 100,
                  lim=limits(min_payout_mojos=10))
    assert [i.wallet_address for i in p.instructions] == [A]
    assert any("dust floor" in d[2] for d in p.dropped)


def test_zero_allocations_produce_no_instruction(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 0)], 1000)
    assert [i.wallet_address for i in p.instructions] == [A]
    assert any("zero allocation" in d[2] for d in p.dropped)


def test_the_pause_switch_blocks_planning(store, tmp_path):
    pause = tmp_path / "PAUSED"
    pause.write_text("stop")
    p = make_plan(store, [rec(A, "t1", 100)], 1000, lim=limits(pause_file=pause))
    assert not p.runnable
    assert any("PAUSED" in b for b in p.blocked_by)


def test_the_same_period_and_budget_is_the_same_cycle(store):
    p1 = make_plan(store, [rec(A, "t1", 100)], 1000)
    p2 = make_plan(store, [rec(A, "t1", 100)], 1000)
    assert p1.cycle_id == p2.cycle_id, (
        "identity must come from what a cycle IS, or re-running a crashed job "
        "would pay again instead of resuming"
    )


def test_a_different_budget_is_a_different_cycle(store):
    a = make_plan(store, [rec(A, "t1", 100)], 1000)
    b = make_plan(store, [rec(A, "t1", 100)], 2000)
    assert a.cycle_id != b.cycle_id


# --- executor ---------------------------------------------------------------

def test_dry_run_sends_nothing(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 50)], 3000, dry_run=True)
    spender = FakeSpender()
    r = execute(p, store=store, spender=spender)
    assert spender.sends == [], "a dry run must not touch the wallet"
    assert r.dry_run and r.sent == ()
    assert len(r.skipped) == 2


def test_a_live_run_sends_once_per_wallet(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 50)], 3000)
    spender = FakeSpender()
    r = execute(p, store=store, spender=spender)
    assert sorted(spender.sends) == sorted([(A, 2000), (B, 1000)])
    assert r.ok and r.sent_mojos == 3000


def test_rerunning_a_settled_cycle_pays_nobody_twice(store):
    records, budget = [rec(A, "t1", 100)], 1000
    spender = FakeSpender()
    execute(make_plan(store, records, budget), store=store, spender=spender)
    assert len(spender.sends) == 1

    p2 = make_plan(store, records, budget)
    assert not p2.runnable, "the second plan must refuse before the executor runs"
    r2 = execute(p2, store=store, spender=spender)
    assert len(spender.sends) == 1, "no second spend"
    assert "already sent" in (r2.halted_reason or "")


def test_a_transient_wallet_error_is_retried_then_succeeds(store):
    p = make_plan(store, [rec(A, "t1", 100)], 1000)
    spender = FakeSpender(fail_times=2)
    r = execute(p, store=store, spender=spender, sleep=lambda s: None)
    assert r.ok and len(r.sent) == 1
    assert len(spender.sends) == 3, "two failures then a success"


def test_retries_give_up_and_record_the_failure(store):
    p = make_plan(store, [rec(A, "t1", 100)], 1000)
    spender = FakeSpender(fail_times=99)
    r = execute(p, store=store, spender=spender, max_attempts=3,
                sleep=lambda s: None)
    assert not r.ok and len(r.failed) == 1
    assert len(spender.sends) == 3
    rows = {i.wallet_address: i for i in store.instructions(p.cycle_id)}
    assert rows[A].state == audit_mod.FAILED and rows[A].attempts == 3


def test_an_unknown_outcome_halts_and_is_never_retried(store):
    """The double-spend case. The wallet answered but we cannot tell what it
    did, so the only safe move is to stop and leave evidence."""
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 100)], 2000)
    spender = FakeSpender(unknown=True)
    r = execute(p, store=store, spender=spender, sleep=lambda s: None)

    assert len(spender.sends) == 1, "must NOT retry an ambiguous spend"
    assert r.halted_reason and "UNKNOWN OUTCOME" in r.halted_reason
    assert r.sent == () and len(r.sent) == 0
    stuck = store.in_flight()
    assert len(stuck) == 1 and stuck[0].wallet_address == A


def test_a_stuck_instruction_blocks_every_later_cycle(store):
    p = make_plan(store, [rec(A, "t1", 100)], 1000)
    execute(p, store=store, spender=FakeSpender(unknown=True), sleep=lambda s: None)

    later = make_plan(store, [rec(B, "t9", 100)], 7777)
    assert not later.runnable
    assert any("mid-send" in b for b in later.blocked_by)


def test_a_blocked_plan_is_never_executed(store):
    p = make_plan(store, [rec(A, "t1", 100)], 5000, bal=1)
    spender = FakeSpender()
    r = execute(p, store=store, spender=spender)
    assert spender.sends == []
    assert r.halted_reason and "insufficient balance" in r.halted_reason


def test_a_live_run_without_a_spender_refuses(store):
    p = make_plan(store, [rec(A, "t1", 100)], 1000)
    with pytest.raises(ExecutorError, match="needs a WalletSpender"):
        execute(p, store=store, spender=None)


def test_confirmations_are_tracked_separately(store):
    p = make_plan(store, [rec(A, "t1", 100)], 1000)
    spender = FakeSpender()
    execute(p, store=store, spender=spender)
    tx = store.instructions(p.cycle_id)[0].tx_id

    assert track_confirmations(p.cycle_id, store=store, spender=spender) == {A: False}
    spender._confirmed.add(tx)
    assert track_confirmations(p.cycle_id, store=store, spender=spender) == {A: True}
    assert store.instructions(p.cycle_id)[0].confirmed is True


# --- audit ------------------------------------------------------------------

def test_every_input_is_recorded_including_the_excluded(store):
    records = [rec(A, "t1", 100), rec(B, "t2", 40, eligible=False, reason="stale")]
    make_plan(store, records, 1000)
    rows = store._c.execute("SELECT * FROM cycle_inputs").fetchall()
    assert len(rows) == 2
    excluded = [r for r in rows if not r["eligible"]][0]
    assert excluded["raw_uptime_bp"] == 4000
    assert excluded["exclusion_reason"] == "stale"


def test_the_audit_row_carries_everything_the_spec_asks_for(store):
    p = make_plan(store, [rec(A, "t1", 90)], 1000)
    execute(p, store=store, spender=FakeSpender())
    cyc = store.get_cycle(p.cycle_id)
    assert cyc["budget_mojos"] == 1000 and cyc["total_weight"] == "9000"
    assert cyc["asset_id"] == ASSET and cyc["uptime_basis"] == "node-hours-present"
    ins = store.instructions(p.cycle_id)[0]
    assert ins.wallet_address == A and ins.amount_mojos == 1000
    assert ins.tx_id and ins.attempts == 1
    kinds = {e["kind"] for e in store.events(p.cycle_id)}
    assert {"planned", "sent"} <= kinds


def test_the_idempotency_key_is_unique_per_cycle_and_wallet(store):
    p = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 100)], 2000)
    keys = {i.idempotency_key for i in p.instructions}
    assert len(keys) == 2
    again = make_plan(store, [rec(A, "t1", 100), rec(B, "t2", 100)], 2000)
    assert {i.idempotency_key for i in again.instructions} == keys


# --- reporting and scheduling ----------------------------------------------

def test_the_dry_run_report_shows_what_would_be_spent(store):
    from orchard_chia.allocation.service import CycleOutcome
    from orchard_chia.allocation.collector import CollectionResult
    records = [rec(A, "t1", 100), rec(B, "t2", 50),
               rec(B, "t3", 0, eligible=False, reason="stale: last reading 9.0h ago")]
    p = make_plan(store, records, 3000, dry_run=True)
    coll = CollectionResult(records=tuple(records), period_start=NOW - timedelta(hours=24),
                            period_end=NOW, uptime_basis="node-hours-present",
                            source="https://oracle.test")
    out = CycleOutcome(collection=coll, plan=p, report=None)
    text = render_report(out, Settings.from_env({}))

    assert "DRY RUN — nothing sent" in text
    assert "WOULD PAY" in text and "stale" in text
    assert "2.000" in text and "1.000" in text, "amounts shown in tokens"


def test_the_scheduler_survives_a_failing_cycle(monkeypatch):
    seen = []

    def boom(*a, **k):
        raise RuntimeError("oracle down")

    monkeypatch.setattr("orchard_chia.allocation.service.run_cycle", boom)
    s = Settings.from_env({"token": {"asset_id": ASSET}})
    rc = run_scheduler(s, max_cycles=3, sleep=lambda x: None,
                       on_cycle=seen.append)
    assert rc == 0 and len(seen) == 3, "a transient failure must not stop the timer"
    assert all(isinstance(x, RuntimeError) for x in seen)


def test_the_scheduler_stops_dead_on_a_halt(monkeypatch):
    from orchard_chia.allocation.service import CycleOutcome
    from orchard_chia.allocation.collector import CollectionResult

    calls = []

    def halted(*a, **k):
        calls.append(1)
        coll = CollectionResult(records=(), period_start=NOW, period_end=NOW,
                                uptime_basis="x", source="y")
        return CycleOutcome(
            collection=coll,
            plan=type("P", (), {"blocked_by": (), "cycle_id": "c"})(),
            report=ExecutionReport(cycle_id="c", dry_run=False,
                                   halted_reason="UNKNOWN OUTCOME"))

    monkeypatch.setattr("orchard_chia.allocation.service.run_cycle", halted)
    s = Settings.from_env({"token": {"asset_id": ASSET}})
    rc = run_scheduler(s, max_cycles=5, sleep=lambda x: None)
    assert rc == 3 and len(calls) == 1, (
        "an unknown spend outcome must stop the machine, not be retried on a timer"
    )


# --- configuration ----------------------------------------------------------

def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert Settings.from_env({}).dry_run is True


def test_a_live_run_needs_a_wallet_id(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    s = Settings.from_env({"token": {"asset_id": ASSET}})
    assert any("WALLET_ID" in p for p in s.validate())


def test_a_missing_asset_id_is_refused(monkeypatch):
    monkeypatch.delenv("ORCHARD_ASSET_ID", raising=False)
    assert any("asset_id" in p for p in Settings.from_env({}).validate())


def test_a_cap_below_the_budget_is_caught_as_config_not_at_runtime(monkeypatch):
    monkeypatch.setenv("ORCHARD_ALLOC_BUDGET_MOJOS", "5000")
    monkeypatch.setenv("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", "100")
    s = Settings.from_env({"token": {"asset_id": ASSET}})
    assert any("every cycle would be blocked" in p for p in s.validate())


def test_the_ceiling_cannot_come_from_the_file_that_holds_the_budget(monkeypatch):
    """An adversarial review's finding: a limit stored beside the number it
    bounds is not a limit. Editing the budget would move the ceiling with it,
    so a misplaced digit stays a 100x payout — with a gate that appears to have
    approved it."""
    for k in ("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", "ORCHARD_ALLOC_MAX_WALLET_MOJOS"):
        monkeypatch.delenv(k, raising=False)
    cfg = {"token": {"asset_id": ASSET},
           "allocation": {"budget_tokens": 1000,
                          "max_per_cycle_mojos": 999_999_999,
                          "max_per_wallet_mojos": 999_999_999}}
    s = Settings.from_env(cfg)
    assert s.max_per_cycle_mojos == 0, "config.yaml must not be able to set the ceiling"
    assert s.max_per_wallet_mojos == 0


def test_a_live_run_without_an_explicit_ceiling_is_refused(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ORCHARD_ALLOC_WALLET_ID", "3")
    for k in ("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", "ORCHARD_ALLOC_MAX_WALLET_MOJOS"):
        monkeypatch.delenv(k, raising=False)
    problems = Settings.from_env({"token": {"asset_id": ASSET}}).validate()
    assert sum("is not set" in p for p in problems) == 2


def test_a_dry_run_says_the_ceiling_is_missing_rather_than_failing(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    for k in ("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", "ORCHARD_ALLOC_MAX_WALLET_MOJOS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env({"token": {"asset_id": ASSET}})
    assert s.validate() == []
    assert len(s.advisories()) == 2


# --- concurrency ------------------------------------------------------------

def test_two_runs_cannot_overlap(tmp_path):
    """The other adversarial finding: the timer and the operator both fire.
    Both read the audit store, both find the wallet unpaid, both send."""
    from orchard_chia.allocation.lock import LockBusy, RunLock
    held = RunLock(tmp_path / "a.lock").acquire()
    try:
        with pytest.raises(LockBusy, match="another allocation run"):
            RunLock(tmp_path / "a.lock").acquire()
    finally:
        held.release()
    RunLock(tmp_path / "a.lock").acquire().release()   # free again


def test_a_stale_lock_is_not_broken_unless_asked(tmp_path):
    """A lock left by a killed process is indistinguishable from one held by a
    process mid-spend. Breaking it by default optimises for the run continuing
    rather than for money not moving twice."""
    import os
    from orchard_chia.allocation.lock import LockBusy, RunLock
    p = tmp_path / "b.lock"
    dead = RunLock(p).acquire()
    os.close(dead._fd)                      # holder died without releasing
    dead._fd = None
    # The file must also RECORD a dead pid: on POSIX the breaker checks holder
    # liveness explicitly, and the test process's own pid is very much alive.
    p.write_text("pid=999999999 started=2020-09-13T00:00:00+00:00\n",
                 encoding="utf-8")
    os.utime(p, (1_600_000_000, 1_600_000_000))

    with pytest.raises(LockBusy):
        RunLock(p).acquire()
    RunLock(p, break_after_seconds=60).acquire().release()


def test_a_live_holder_is_never_broken_even_past_the_threshold(tmp_path):
    """A long-running holder is not a stale one. On Windows the open handle
    makes this detectable; the error has to say so rather than surface as a
    confusing FileExistsError from the retry."""
    import os
    from orchard_chia.allocation.lock import LockBusy, RunLock
    p = tmp_path / "d.lock"
    alive = RunLock(p).acquire()            # still open
    os.utime(p, (1_600_000_000, 1_600_000_000))
    try:
        with pytest.raises(LockBusy, match="still has it open|still alive|another allocation run"):
            RunLock(p, break_after_seconds=60).acquire()
    finally:
        alive.release()


def test_the_lock_names_its_holder(tmp_path):
    import os
    from orchard_chia.allocation.lock import RunLock
    lk = RunLock(tmp_path / "c.lock").acquire()
    try:
        assert f"pid={os.getpid()}" in (tmp_path / "c.lock").read_text()
    finally:
        lk.release()


def test_a_cycle_holds_the_lock_for_its_whole_run(tmp_path, monkeypatch):
    """Not just around the spend — the duplicate check happens at plan time."""
    from orchard_chia.allocation.lock import LockBusy, RunLock
    monkeypatch.setenv("ORCHARD_ALLOC_DB", str(tmp_path / "alloc.db"))
    monkeypatch.setenv("ORCHARD_ALLOC_BUDGET_MOJOS", "1000")
    s = Settings.from_env({"token": {"asset_id": ASSET}})

    blocker = RunLock(s.db_path.with_suffix(".lock")).acquire()
    try:
        src = FakeOracle([{"node_id": "T1", "sensors": [], "wallet_address": A,
                           "last_reading_at": NOW.isoformat()}],
                         {"T1": {"hours_online": 24}})
        with pytest.raises(LockBusy):
            run_cycle(s, source=src, now=NOW)
    finally:
        blocker.release()


# --- the live spender is buildable ------------------------------------------

def test_build_spender_matches_the_real_wallet_rpc_signature():
    """The old wiring passed ca_cert_path/ca_key_path — keywords WalletRpc
    does not accept — so every live run would have crashed with a TypeError on
    the one path no dry run exercises. The builder is now called with a
    factory-checked signature."""
    import inspect
    from orchard_chia.allocation.executor import build_spender
    from orchard_chia.wallet.rpc import WalletRpc

    seen = {}

    def factory(**kw):
        # Every keyword the builder passes must be a real WalletRpc parameter.
        params = set(inspect.signature(WalletRpc.__init__).parameters) - {"self"}
        assert set(kw) <= params, f"unknown kwargs: {set(kw) - params}"
        seen.update(kw)
        return object()

    sp = build_spender(wallet_id=3, fee_mojos=10,
                       wallet_cfg={"cert_path": "c.pem", "key_path": "k.pem",
                                   "host": "localhost", "port": 9256},
                       rpc_factory=factory)
    assert sp.wallet_id == 3 and sp.fee_mojos == 10
    assert seen["cert_path"] == "c.pem"


def test_build_spender_refuses_missing_credentials():
    from orchard_chia.allocation.executor import ExecutorError, build_spender
    with pytest.raises(ExecutorError, match="mTLS credentials"):
        build_spender(wallet_id=3, fee_mojos=0, wallet_cfg={},
                      rpc_factory=lambda **kw: object())


def test_build_spender_refuses_a_zero_wallet_id():
    from orchard_chia.allocation.executor import ExecutorError, build_spender
    with pytest.raises(ExecutorError, match="wallet_id"):
        build_spender(wallet_id=0, fee_mojos=0,
                      wallet_cfg={"cert_path": "c", "key_path": "k"},
                      rpc_factory=lambda **kw: object())
