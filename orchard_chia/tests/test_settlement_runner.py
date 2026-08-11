# SPDX-License-Identifier: Apache-2.0
"""The daily settlement runner: closed seasons only, ledger first, dry by default."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from orchard_chia.economics import PoolLedger, TREE_REWARDS_POOL_MOJOS
from orchard_chia.economics.runner import (day_index_for_season, main,
                                           observe_season)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
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


def test_a_cloned_device_key_disqualifies_every_claimant():
    """One physical board earning as two Trees. Both go ineligible — between a
    clone and its original the oracle cannot tell which is the imposter, and
    the honest operator is the one who can fix it."""
    pk = "02" + "ab" * 32
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"],
          "device_pubkey": pk},
         {"node_id": "T2", "wallet_address": W, "sensors": ["s"],
          "device_pubkey": pk},
         {"node_id": "T3", "wallet_address": W, "sensors": ["s"],
          "device_pubkey": "02" + "cd" * 32}],
        {"T1": {"hours_online": 24}, "T2": {"hours_online": 24},
         "T3": {"hours_online": 24}})
    trees = observe_season(src, 74)
    by_id = {t.tree_id: t for t in trees}
    assert not by_id["T1"].eligible and not by_id["T2"].eligible
    assert "one board, one identity" in by_id["T1"].ineligible_reason
    assert by_id["T3"].eligible, "an honest Tree is untouched"


def test_nodes_without_a_pubkey_are_not_treated_as_clones_of_each_other():
    """Absent is not equal: two legacy nodes with no pubkey share nothing."""
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"]},
         {"node_id": "T2", "wallet_address": W, "sensors": ["s"]}],
        {"T1": {"hours_online": 24}, "T2": {"hours_online": 24}})
    trees = observe_season(src, 74)
    assert all(t.eligible for t in trees)


def test_a_sealed_season_on_chain_dominates_the_oracle(monkeypatch, tmp_path):
    """Once the chain holds a seal, it is the truth. A proof-backed seal pays
    its verified_hours; the oracle's larger self-report is ignored."""
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "1")

    class Att:
        node_id = "T1"
        season = 74
        signed = {"verified_hours": 9, "hours_online": 24,
                  "seal_source": "readings", "sigs_verified": True}

    monkeypatch.setattr(
        "orchard_chia.economics.runner._chain_hours_for_season",
        lambda season: {"T1": (9, "chain:verified_hours")})
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"],
          "last_reading_at": NOW.isoformat()}],
        {"T1": {"hours_online": 24}})
    trees = observe_season(src, 74)
    assert trees[0].verified_heartbeats == 9
    assert trees[0].heartbeat_basis == "chain:verified_hours"


def test_a_sealed_placeholder_pays_zero_not_the_oracle_claim(monkeypatch):
    """A sealed season with no evidence is worth zero — the honest answer for
    that season, not a fallback to the oracle's word."""
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "1")
    monkeypatch.setattr(
        "orchard_chia.economics.runner._chain_hours_for_season",
        lambda season: {"T1": (0, "chain:unproven (placeholder)")})
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"],
          "last_reading_at": NOW.isoformat()}],
        {"T1": {"hours_online": 24}})
    trees = observe_season(src, 74)
    assert trees[0].verified_heartbeats == 0
    assert "placeholder" in trees[0].heartbeat_basis


def test_an_unsealed_season_falls_back_to_oracle_hours(monkeypatch):
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "1")
    monkeypatch.setattr(
        "orchard_chia.economics.runner._chain_hours_for_season",
        lambda season: {})
    src = FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"],
          "last_reading_at": NOW.isoformat()}],
        {"T1": {"hours_online": 18}})
    trees = observe_season(src, 74)
    assert trees[0].verified_heartbeats == 18
    assert trees[0].heartbeat_basis == "oracle-hours"


def test_a_recently_retired_tree_still_earns_its_past_season(monkeypatch):
    """Retirement ends the future, not the history. The retire flow promises
    'nothing it produced is deleted'; settlement must not confiscate a season
    the Tree demonstrably ran."""
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "0")   # testing retirement

    class RetiringOracle(FakeOracle):
        def list_nodes(self, include_retired=False):
            live = [{"node_id": "LIVE", "wallet_address": W, "sensors": ["s"]}]
            if include_retired:
                return live + [{"node_id": "GONE", "wallet_address": W,
                                "sensors": ["s"]}]
            return live

    src = RetiringOracle([], {"LIVE": {"hours_online": 24},
                              "GONE": {"hours_online": 20}})
    trees = observe_season(src, 74)
    by_id = {t.tree_id: t for t in trees}
    assert "GONE" in by_id, "the retired Tree must be observed for its past"
    assert by_id["GONE"].verified_heartbeats == 20


def test_a_long_dead_ghost_still_earns_nothing(monkeypatch):
    """Including retired Trees is not a payout to ghosts: a Tree with no hours
    that season earns zero through the ordinary uptime rule."""
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "0")   # testing retirement

    class RetiringOracle(FakeOracle):
        def list_nodes(self, include_retired=False):
            if include_retired:
                return [{"node_id": "GHOST", "wallet_address": W,
                         "sensors": ["s"]}]
            return []

    src = RetiringOracle([], {"GHOST": {"hours_online": 0}})
    trees = observe_season(src, 74)
    assert trees[0].verified_heartbeats == 0


def test_the_report_shows_each_trees_basis(monkeypatch, capsys):
    """The reviewer deciding on --yes must see whether a number is
    chain-verified or oracle-trusted, per Tree, in the report itself."""
    from orchard_chia.economics.runner import render
    from orchard_chia.economics import TreeDay, settle_day
    t = TreeDay(tree_id="T1", wallet_address=W, qualifying_sensors=1,
                verified_heartbeats=9, heartbeat_basis="chain:verified_hours")
    s = settle_day([t], day_index=0, pool_remaining_mojos=10**9)
    text = render(s, season=1, dry=True)
    assert "[chain:verified_hours]" in text


# --- the chain is consulted by default, and a failed consult refuses --------

def test_the_chain_is_consulted_by_default(monkeypatch):
    """'Don't trust the oracle, verify it' — paying on the oracle's word while
    a signed seal for that season sits on chain contradicts the thesis."""
    monkeypatch.delenv("ORCHARD_SETTLE_CHAIN", raising=False)
    called = {}
    import orchard_chia.economics.runner as R

    def _spy(season):
        called["yes"] = True
        return {}

    monkeypatch.setattr(R, "_chain_hours_for_season", _spy)
    src = FakeOracle([{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}],
                     {"T1": {"hours_online": 12}})
    R.observe_season(src, 74)
    assert called.get("yes"), "the chain must be consulted without opting in"


def test_it_can_be_turned_off_deliberately(monkeypatch):
    """For a host with no DataLayer daemon."""
    monkeypatch.setenv("ORCHARD_SETTLE_CHAIN", "0")
    from orchard_chia.economics.runner import _chain_hours_for_season
    assert _chain_hours_for_season(74) == {}


def test_a_failed_consult_refuses_instead_of_falling_back(monkeypatch):
    """The fallback is not neutral: the chain's figure is never HIGHER than
    the oracle's, so reverting on a transient hiccup can only overpay — and a
    day settles once, so the overpayment is permanent."""
    import orchard_chia.economics.runner as R
    monkeypatch.delenv("ORCHARD_SETTLE_CHAIN", raising=False)
    monkeypatch.setattr(R, "_chain_hours_for_season",
                        lambda s: (_ for _ in ()).throw(
                            R.ChainConsultError("datalayer refused")))
    src = FakeOracle([{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}],
                     {"T1": {"hours_online": 24}})
    with pytest.raises(R.ChainConsultError):
        R.observe_season(src, 74)


def test_the_refusal_reaches_the_cli_as_an_exit_code(monkeypatch, tmp_path, capsys):
    import orchard_chia.economics.runner as R
    monkeypatch.setenv("ORCHARD_POOL_LEDGER", str(tmp_path / "c.db"))
    monkeypatch.setenv("ORCHARD_ASSET_ID", "ab" * 32)
    monkeypatch.setattr(R, "OracleClient", lambda url, tok: FakeOracle(
        [{"node_id": "T1", "wallet_address": W, "sensors": ["s"]}],
        {"T1": {"hours_online": 24}}))
    monkeypatch.setattr(R, "schedule", type("S", (), {
        "season_number_for": staticmethod(lambda now: 77),
        "season_genesis_from_env": staticmethod(
            lambda: __import__("datetime").date(2026, 5, 27))})())
    monkeypatch.setattr(R, "_chain_hours_for_season",
                        lambda s: (_ for _ in ()).throw(
                            R.ChainConsultError("datalayer refused")))
    assert R.main(["settle", "--season", "76", "--yes"]) == 3
    # The stub's own message must reach stderr — asserting the PRODUCTION
    # wording here would only be asserting the text this test substituted.
    assert "datalayer refused" in capsys.readouterr().err


def test_the_real_refusal_explains_why_falling_back_would_overpay(monkeypatch):
    """Separate from the CLI test, which replaces the message it would check.

    Triggered, not source-inspected: source text contains the implicit
    string-concatenation syntax between fragments, so a phrase spanning two
    lines never matches however the whitespace is normalised. The message a
    person actually reads is the thing worth asserting.
    """
    from orchard_chia.economics import runner
    monkeypatch.delenv("ORCHARD_SETTLE_CHAIN", raising=False)
    # The sandboxed config makes the consult fail for real.
    with pytest.raises(runner.ChainConsultError) as got:
        runner._chain_hours_for_season(74)
    msg = str(got.value)
    assert "Refusing to fall back" in msg
    assert "can only overpay" in msg
    assert "ORCHARD_SETTLE_CHAIN=0" in msg


def test_the_chain_is_read_once_per_process_not_once_per_season(monkeypatch):
    """settle --all over 76 seasons used to trigger 76 full store scans and
    simply stopped responding. The store is append-only and a settle run is
    short, so one snapshot is correct as well as fast."""
    import orchard_chia.economics.runner as R
    monkeypatch.delenv("ORCHARD_SETTLE_CHAIN", raising=False)
    R.reset_chain_index()
    reads = {"n": 0}

    def _fake_load():
        reads["n"] += 1
        return {74: {"T1": (9, "chain:verified_hours")},
                75: {"T1": (24, "chain:verified_hours")}}

    monkeypatch.setattr(R, "_load_chain_index", _fake_load)
    assert R._chain_hours_for_season(74) == {"T1": (9, "chain:verified_hours")}
    assert R._chain_hours_for_season(75) == {"T1": (24, "chain:verified_hours")}
    assert R._chain_hours_for_season(76) == {}          # sealed nothing
    assert reads["n"] == 1, f"{reads['n']} store scans for three seasons"
    R.reset_chain_index()


def test_the_cached_index_cannot_be_mutated_by_a_caller(monkeypatch):
    """A caller editing the returned dict must not corrupt later seasons."""
    import orchard_chia.economics.runner as R
    monkeypatch.delenv("ORCHARD_SETTLE_CHAIN", raising=False)
    R.reset_chain_index()
    monkeypatch.setattr(R, "_load_chain_index",
                        lambda: {74: {"T1": (9, "chain:verified_hours")}})
    got = R._chain_hours_for_season(74)
    got["T1"] = (999, "tampered")
    assert R._chain_hours_for_season(74)["T1"] == (9, "chain:verified_hours")
    R.reset_chain_index()
