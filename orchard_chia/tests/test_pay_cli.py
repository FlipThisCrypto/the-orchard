# SPDX-License-Identifier: Apache-2.0
"""The pay command: dry by default, two acts to go live, ceilings external."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchard_chia.economics import PoolLedger, TreeDay, settle_day
from orchard_chia.economics.runner import main

W = "xch1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ASSET = "285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121"


@pytest.fixture()
def settled(tmp_path, monkeypatch):
    path = tmp_path / "pool.db"
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(path))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.delenv("DRY_RUN", raising=False)
    with PoolLedger(path) as led:
        led.record(settle_day(
            [TreeDay(tree_id="T1", wallet_address=W, qualifying_sensors=1,
                     verified_heartbeats=24)],
            day_index=0, pool_remaining_mojos=5_000_000))
    return path


def test_pay_is_dry_by_default(settled, capsys):
    assert main(["pay"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "5,000.000" in out


def test_a_dry_run_leaves_the_day_unpaid(settled, capsys):
    main(["pay"]); main(["pay"])
    out = capsys.readouterr().out
    assert out.count("DRY RUN") == 2, "a dry run must not consume the day"


def test_live_needs_both_acts(settled, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    assert main(["pay"]) == 2
    assert "was not given" in capsys.readouterr().err


def test_live_needs_external_ceilings(settled, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    for k in ("ORCHARD_PAY_MAX_CYCLE_MOJOS", "ORCHARD_PAY_MAX_WALLET_MOJOS"):
        monkeypatch.delenv(k, raising=False)
    rc = main(["pay", "--i-understand-this-spends-real-tokens"])
    assert rc == 2
    assert "ceilings" in capsys.readouterr().err


def test_no_asset_id_refuses_even_dry(settled, monkeypatch, capsys):
    """Neither env nor config: still a refusal. (The config fallback added
    later means this test must silence BOTH sources, not just the env one.)"""
    monkeypatch.delenv("ORCHARD_ASSET_ID", raising=False)
    monkeypatch.setattr("orchard_chia.allocation.__main__._load_config",
                        lambda: {})
    assert main(["pay"]) == 2
    assert "Refusing to guess which CAT" in capsys.readouterr().err


def test_nothing_unpaid_is_a_clean_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "empty.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert main(["pay"]) == 0
    assert "no settled unpaid days" in capsys.readouterr().out


def test_status_reads_the_ledger(settled, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "POOL" in out and "remaining" in out
    assert "1 settled day(s) unpaid" in out


def test_status_on_a_fresh_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "f.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "85,000,000.000" in out and "never" in out and "nothing owed" in out


def test_status_surfaces_a_stuck_payment(settled, tmp_path, monkeypatch, capsys):
    """A mid-send instruction blocks every later pay; the operator's first
    stop must say so rather than leave them to find out by being refused."""
    from orchard_chia.allocation import audit as audit_mod
    audit_path = tmp_path / "pool.db"
    audit_path = audit_path.with_name("payment_audit.db")
    with audit_mod.AuditStore(audit_path) as store:
        store.open_cycle(
            cycle_id="c" * 32, period_start=datetime(2026, 5, 27, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 28, tzinfo=timezone.utc),
            budget_mojos=1000, allocated_mojos=1000, total_weight="1",
            asset_id=ASSET, uptime_basis="economics-ledger", dry_run=False)
        store.put_instruction(cycle_id="c" * 32, wallet_address=W,
                              amount_mojos=1000, wallet_avg_uptime="0",
                              pair_count=1)
        store.mark_sending("c" * 32, W)

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "MID-SEND" in out and "blocked until resolved" in out


def test_settle_all_is_dry_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "a.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setattr(
        "orchard_chia.economics.runner.OracleClient",
        lambda url, tok: _FakeOracleAll())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 4)
    assert main(["settle", "--all"]) == 0
    out = capsys.readouterr().out
    assert "3 closed season(s) to settle: 1..3" in out and "DRY RUN" in out
    with PoolLedger(tmp_path / "a.db") as led:
        assert led.snapshot().days_settled == 0


class _FakeOracleAll:
    def list_nodes(self):
        return [{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}]

    def get_uptime(self, node_id, season):
        return {"hours_online": 24}


def test_settle_all_with_yes_records_everything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "b.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setattr(
        "orchard_chia.economics.runner.OracleClient",
        lambda url, tok: _FakeOracleAll())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 4)
    assert main(["settle", "--all", "--yes"]) == 0
    with PoolLedger(tmp_path / "b.db") as led:
        assert led.snapshot().days_settled == 3
    capsys.readouterr()
    assert main(["settle", "--all", "--yes"]) == 0
    assert "nothing to settle" in capsys.readouterr().out


def test_settle_without_season_or_all_explains(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "c.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    assert main(["settle"]) == 2
    assert "--season N or --all" in capsys.readouterr().err


def test_a_crash_between_send_and_mark_paid_heals(settled, tmp_path,
                                                  monkeypatch, capsys):
    """Simulate the wedge: pay live, executor sends, mark_paid never runs.
    The next pay must recognise the fully-sent cycle and record it, not refuse
    forever."""
    from orchard_chia.allocation import audit as audit_mod
    from orchard_chia.allocation.executor import execute
    from orchard_chia.allocation.planner import PlannerLimits
    from orchard_chia.economics import payment
    from orchard_chia.economics.runner import _cmd_pay  # noqa: F401 (import check)

    class Spender:
        def spendable_balance(self):
            return 10**12

        def send(self, ins):
            return "0xtxdead"

        def confirmed(self, tx):
            return True

    ledger_path = tmp_path / "pool.db"
    audit_path = ledger_path.with_name("payment_audit.db")
    genesis = datetime(2026, 5, 27, tzinfo=timezone.utc)
    with PoolLedger(ledger_path) as led, audit_mod.AuditStore(audit_path) as store:
        dp = payment.plan_day_payment(
            led, 0, store=store, asset_id=ASSET, genesis=genesis,
            limits=PlannerLimits(max_per_cycle_mojos=10**12,
                                 max_per_wallet_mojos=10**12),
            available_balance_mojos=10**12, dry_run=False)
        report = execute(dp.plan, store=store, spender=Spender())
        assert report.ok
        # crash here: mark_paid never runs.

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ORCHARD_PAY_MAX_CYCLE_MOJOS", str(10**12))
    monkeypatch.setenv("ORCHARD_PAY_MAX_WALLET_MOJOS", str(10**12))
    monkeypatch.setenv("ORCHARD_PAY_WALLET_ID", "3")
    rc = main(["pay", "--day", "0", "--i-understand-this-spends-real-tokens"])
    out = capsys.readouterr().out
    assert rc == 0 and "healed" in out

    with PoolLedger(ledger_path) as led:
        row = led._c.execute(
            "SELECT paid_at, paid_cycle FROM settled_days WHERE day_index=0"
        ).fetchone()
        assert row["paid_at"] and row["paid_cycle"]


def test_settle_writes_an_ops_journal_entry(tmp_path, monkeypatch):
    """The commands that move value journal like the ones that move data."""
    import json
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "j.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path / "ops"))
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(
        "orchard_chia.economics.runner.OracleClient",
        lambda url, tok: _FakeOracleAll())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 3)
    assert main(["settle", "--season", "1", "--yes"]) == 0
    journal = tmp_path / "ops" / "settle.jsonl"
    assert journal.exists()
    events = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "finish" and e.get("season") == 1
               for e in events) or any(e.get("season") == 1 for e in events)


def test_status_warns_when_the_ledger_fails_its_own_audit(settled, monkeypatch,
                                                          capsys):
    """audit-on-demand only protects those who remember it exists; status is
    what people actually run."""
    import os
    path = os.environ["ORCHARD_POOL_LEDGER"]
    with PoolLedger(path) as led:
        led._c.execute("UPDATE settled_days SET pool_closing_mojos = "
                       "pool_closing_mojos - 1 WHERE day_index=0")
        led._c.commit()
    assert main(["status"]) == 0
    assert "FAILS ITS OWN AUDIT" in capsys.readouterr().out


def test_an_absurd_fee_is_refused(monkeypatch):
    """Fees are XCH, not JUICE — no JUICE ceiling covers them, and two extra
    zeros would burn real money per instruction, silently."""
    from orchard_chia.economics.runner import _fee_mojos
    monkeypatch.setenv("ORCHARD_PAY_FEE_MOJOS", str(10**11))  # 0.1 XCH
    monkeypatch.delenv("ORCHARD_PAY_FEE_CAP_ACK", raising=False)
    with pytest.raises(SystemExit, match="sanity cap"):
        _fee_mojos()


def test_a_sane_fee_passes_and_the_ack_lifts_the_cap(monkeypatch):
    from orchard_chia.economics.runner import _fee_mojos
    monkeypatch.setenv("ORCHARD_PAY_FEE_MOJOS", "100000000")   # 0.0001 XCH
    assert _fee_mojos() == 100_000_000
    monkeypatch.setenv("ORCHARD_PAY_FEE_MOJOS", str(10**11))
    monkeypatch.setenv("ORCHARD_PAY_FEE_CAP_ACK", "i-know")
    assert _fee_mojos() == 10**11


def test_a_negative_fee_is_refused(monkeypatch):
    from orchard_chia.economics.runner import _fee_mojos
    monkeypatch.setenv("ORCHARD_PAY_FEE_MOJOS", "-1")
    with pytest.raises(SystemExit, match="negative"):
        _fee_mojos()


class _BlindOracle:
    """Real hours, no visible wallet — the scheduler-without-token shape."""
    def list_nodes(self, include_retired=False):
        return [{"node_id": "T1", "wallet_address": None, "sensors": ["s"]}]

    def get_uptime(self, node_id, season):
        return {"hours_online": 9}


def test_a_wallet_blind_settle_refuses_rather_than_burning_the_day(
        tmp_path, monkeypatch, capsys):
    """The day settles once. Recording 0 for hours genuinely earned, because
    the CALLER could not see wallets, would burn those rewards forever."""
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "g.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setattr("orchard_chia.economics.runner.OracleClient",
                        lambda url, tok: _BlindOracle())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 77)
    rc = main(["settle", "--season", "76", "--yes"])
    assert rc == 4
    assert "ORCHARD_ORACLE_WRITER_TOKEN" in capsys.readouterr().err
    with PoolLedger(tmp_path / "g.db") as led:
        assert led.snapshot().days_settled == 0, "nothing may be recorded"


def test_settle_all_stops_at_a_wallet_blind_season(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "h.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setattr("orchard_chia.economics.runner.OracleClient",
                        lambda url, tok: _BlindOracle())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 77)
    rc = main(["settle", "--all", "--yes"])
    assert rc == 4
    with PoolLedger(tmp_path / "h.db") as led:
        assert led.snapshot().days_settled == 0


def test_a_genuinely_hour_less_tree_with_no_wallet_still_settles(
        tmp_path, monkeypatch, capsys):
    """Zero hours and no wallet is just an idle unclaimed Tree — the day
    settles normally; nothing was earned to burn."""
    class Idle:
        def list_nodes(self, include_retired=False):
            return [{"node_id": "T1", "wallet_address": None, "sensors": ["s"]}]
        def get_uptime(self, node_id, season):
            return {"hours_online": 0}

    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "i.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", ASSET)
    monkeypatch.setattr("orchard_chia.economics.runner.OracleClient",
                        lambda url, tok: Idle())
    monkeypatch.setattr(
        "orchard_chia.economics.runner.schedule.season_number_for",
        lambda now: 77)
    assert main(["settle", "--season", "76", "--yes"]) == 0
    with PoolLedger(tmp_path / "i.db") as led:
        assert led.snapshot().days_settled == 1


def test_the_asset_id_falls_back_to_config(tmp_path, monkeypatch, capsys):
    """The operator's config already names the token; demanding the env var
    too was duplication. "Never GUESS which CAT" is the property — reading the
    configured value is not a guess."""
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "a.db"))
    monkeypatch.delenv("ORCHARD_ASSET_ID", raising=False)
    monkeypatch.setattr("orchard_chia.allocation.__main__._load_config",
                        lambda: {"token": {"asset_id": ASSET}})
    assert main(["pay"]) == 0
    assert "no settled unpaid days" in capsys.readouterr().out


def test_neither_source_still_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "b.db"))
    monkeypatch.delenv("ORCHARD_ASSET_ID", raising=False)
    monkeypatch.setattr("orchard_chia.allocation.__main__._load_config",
                        lambda: {})
    assert main(["pay"]) == 2
    assert "Refusing to guess which CAT" in capsys.readouterr().err


# --- per-Tree liveness ------------------------------------------------------

class _Seen:
    def __init__(self, minutes_ago):
        from datetime import datetime, timedelta, timezone as tz
        self._when = (None if minutes_ago is None else
                      (datetime.now(tz.utc) - timedelta(minutes=minutes_ago)))

    def list_nodes(self, include_retired=False):
        return [{"node_id": "D8641AD6CAE36977818499469F7E8C49",
                 "last_reading_at": self._when.isoformat() if self._when else None}]


def test_a_reporting_tree_reads_as_reporting():
    from orchard_chia.economics.runner import _tree_liveness
    assert "reporting" in _tree_liveness(_Seen(3))[0]


def test_a_quiet_tree_is_flagged():
    from orchard_chia.economics.runner import _tree_liveness
    line = _tree_liveness(_Seen(45))[0]
    assert "QUIET" in line and "earning nothing" in line


def test_a_dark_tree_names_the_unearnable_hours():
    """Every hour dark is unearnable and unrecoverable — a season settles once
    and cannot be backfilled."""
    from orchard_chia.economics.runner import _tree_liveness
    line = _tree_liveness(_Seen(15 * 60))[0]
    assert "DARK" in line and "unearnable" in line


def test_a_tree_that_never_reported_says_so():
    from orchard_chia.economics.runner import _tree_liveness
    assert "never reported" in _tree_liveness(_Seen(None))[0]


def test_an_unreadable_oracle_does_not_break_status():
    from orchard_chia.economics.runner import _tree_liveness

    class Dead:
        def list_nodes(self, include_retired=False):
            raise RuntimeError("oracle down")

    assert "could not read Trees" in _tree_liveness(Dead())[0]


def test_clock_skew_does_not_render_negative_minutes():
    """The oracle's clock runs slightly ahead; "-0 min ago" reads like a bug
    and trains the operator to distrust the whole line."""
    from orchard_chia.economics.runner import _tree_liveness
    line = _tree_liveness(_Seen(-1))[0]        # timestamp in the future
    assert "-" not in line.split("(")[-1]
    assert "reporting" in line
