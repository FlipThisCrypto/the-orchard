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
    monkeypatch.delenv("ORCHARD_ASSET_ID", raising=False)
    assert main(["pay"]) == 2
    assert "refusing to guess which CAT" in capsys.readouterr().err


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
