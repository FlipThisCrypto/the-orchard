# SPDX-License-Identifier: Apache-2.0
"""The bridge from oracle observations to wallet payables.

The mapping decisions here change money, so each is pinned: how heartbeats are
derived, how sensors are counted, and what happens to a Tree the reward layer
would refuse outright.
"""
from __future__ import annotations

from orchard_chia.economics import (DAILY_EMISSION_MOJOS_BY_YEAR,
                                    TREE_REWARDS_POOL_MOJOS, Settlement,
                                    settle_day, tree_day_from_observation)

W = "xch1wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww"
YEAR1 = DAILY_EMISSION_MOJOS_BY_YEAR[1]


def obs(tid="T1", wallet=W, sensors=("ds18b20",), hours=24, **kw):
    return tree_day_from_observation(
        tree_id=tid, wallet_address=wallet,
        declared_sensors=list(sensors), hours_with_readings=hours, **kw)


def test_hours_map_to_heartbeats():
    assert obs(hours=18).verified_heartbeats == 18
    assert obs(hours=18).uptime_factor.numerator == 3


def test_season_hours_beyond_a_day_are_clamped_not_refused():
    """The oracle reports season totals; near a boundary they can exceed 24.
    An upstream overclaim caps this Tree's reward, never crashes the day."""
    assert obs(hours=900).verified_heartbeats == 24
    assert obs(hours=-3).verified_heartbeats == 0


def test_sensor_names_are_deduplicated():
    """Declaring the same name twice is not two sensors — redundant
    declarations must not farm the bonus."""
    t = obs(sensors=("ds18b20", "ds18b20", "gps"))
    assert t.qualifying_sensors == 2


def test_a_tree_with_no_declared_sensors_gets_zero_weight():
    t = obs(sensors=())
    assert t.qualifying_sensors == 0


def test_an_eligible_tree_without_a_wallet_becomes_ineligible_with_reason():
    """TreeDay refuses that combination outright; the bridge downgrades it so
    one unclaimed Tree cannot crash everyone's settlement."""
    t = tree_day_from_observation(
        tree_id="t1", wallet_address=None, declared_sensors=["ds18b20"],
        hours_with_readings=24)
    assert t.eligible is False
    assert "wallet" in t.ineligible_reason


def test_node_ids_are_normalised_to_upper():
    assert obs(tid="abcd").tree_id == "ABCD"


def test_settle_day_runs_the_whole_pipeline():
    s = settle_day([obs()], day_index=0,
                   pool_remaining_mojos=TREE_REWARDS_POOL_MOJOS)
    assert isinstance(s, Settlement)
    assert s.distributed_mojos == YEAR1
    assert s.pool_closing_mojos == TREE_REWARDS_POOL_MOJOS - YEAR1
    assert s.payable_by_wallet == {W: YEAR1}


def test_settlement_preserves_the_unearned_accounting():
    s = settle_day([obs(hours=12)], day_index=0,
                   pool_remaining_mojos=TREE_REWARDS_POOL_MOJOS)
    assert s.unearned_mojos == YEAR1 - YEAR1 // 2
    assert s.pool_closing_mojos == TREE_REWARDS_POOL_MOJOS - YEAR1 // 2


def test_settlement_carries_the_model_version():
    s = settle_day([obs()], day_index=0, pool_remaining_mojos=YEAR1)
    from orchard_chia.economics import MODEL_VERSION
    assert s.model_version == MODEL_VERSION
