# SPDX-License-Identifier: Apache-2.0
"""The daily settlement runner: closed seasons only, ledger first, dry by default."""
from __future__ import annotations

import pytest

from orchard_chia.economics import PoolLedger, TREE_REWARDS_POOL_MOJOS
from orchard_chia.economics.runner import (day_index_for_season, main,
                                           observe_season)

W = "xch1wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww"


class FakeOracle:
    def __init__(self, nodes, uptimes):
        self._nodes, self._uptimes = nodes, uptimes

    def list_nodes(self):
        return self._nodes

    def get_uptime(self, node_id, season):
        from orchard_chia.datalayer.oracle import OracleError
        if node_id not in self._uptimes:
            raise OracleError("no uptime")
        return self._uptimes[node_id]


def test_seasons_map_to_days_zero_based():
    assert day_index_for_season(1) == 0
    assert day_index_for_season(74) == 73
    with pytest.raises(ValueError):
        day_index_for_season(0)


def test_observation_builds_a_tree_day_per_node():
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["ds18b20"]}],
        {"T1": {"hours_online": 18}})
    trees = observe_season(src, 74)
    assert len(trees) == 1
    assert trees[0].verified_heartbeats == 18 and trees[0].eligible


def test_an_unreadable_tree_is_ineligible_not_fatal():
    """One broken uptime read must not zero everyone else's settlement."""
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["ds18b20"]},
         {"node_id": "T2", "wallet_address": W, "sensors": ["ds18b20"]}],
        {"T1": {"hours_online": 24}})
    trees = observe_season(src, 74)
    ok = [t for t in trees if t.eligible]
    bad = [t for t in trees if not t.eligible]
    assert [t.tree_id for t in ok] == ["T1"]
    assert "unreadable" in bad[0].ineligible_reason


def test_an_open_season_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "l.db"))
    rc = main(["settle", "--season", "99999", "--yes"])
    assert rc == 2
    assert "not closed" in capsys.readouterr().err


def test_settle_is_dry_without_yes(tmp_path, monkeypatch, capsys):
    """The 185-placeholder lesson: writing must take an explicit act."""
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "l.db"))
    monkeypatch.setattr(
        "orchard_chia.economics.runner.OracleClient",
        lambda url, tok: FakeOracle(
            [{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}],
            {"T1": {"hours_online": 24}}))
    rc = main(["settle", "--season", "1"])
    out = capsys.readouterr().out
    assert rc == 0 and "Dry run" in out
    with PoolLedger(tmp_path / "l.db") as led:
        assert led.snapshot().days_settled == 0, "nothing may be recorded"


def test_settle_with_yes_records_and_a_rerun_reports_already_settled(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "l.db"))
    monkeypatch.setattr(
        "orchard_chia.economics.runner.OracleClient",
        lambda url, tok: FakeOracle(
            [{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}],
            {"T1": {"hours_online": 12}}))
    assert main(["settle", "--season", "1", "--yes"]) == 0
    with PoolLedger(tmp_path / "l.db") as led:
        snap = led.snapshot()
        assert snap.days_settled == 1
        assert snap.remaining_mojos < TREE_REWARDS_POOL_MOJOS

    capsys.readouterr()
    assert main(["settle", "--season", "1", "--yes"]) == 0
    assert "already settled" in capsys.readouterr().out
