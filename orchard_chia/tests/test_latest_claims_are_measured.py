# SPDX-License-Identifier: Apache-2.0
"""``latest:`` must not claim more than the store can prove.

The first real publish (2026-08-08, node D8641AD6…) wrote this to mainnet,
permanently and publicly:

    {"running_hours_online": 15, "last_sealed_season": 73, ...}

Neither number was measured.

* ``running_hours_online`` was ``hour + 1`` — the UTC hour-of-day INDEX. One
  published hour that happened to be numbered 14 asserted "15 hours online".
  The store proved exactly ONE hour, and the node had only existed for 12.25
  hours, so the claim exceeded even the physical ceiling.
* ``last_sealed_season`` was ``current_season - 1``, unchecked. The store held
  ZERO attest records for that node, so it asserted a seal that does not exist.

This is the precise failure the project exists to prevent: a public, permanent,
"verifiable" record asserting numbers nobody can verify — and which are wrong.
A dataset that overstates in the small is not trusted in the large.

Both values are now injected as measurements, and default to the honest answer
(0 / None) when they cannot be measured.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from orchard_chia.datalayer import publish, schedule
from orchard_chia.datalayer.publish_watermark import PublishWatermark

NODE = "D8641AD6CAE36977818499469F7E8C49"


def _wm() -> PublishWatermark:
    return PublishWatermark(Path(tempfile.mkdtemp()) / "wm.db")


# --- the measurement itself ------------------------------------------------
def test_published_hours_count_starts_at_zero():
    wm = _wm()
    assert wm.published_hours_count(NODE, 74) == 0
    wm.close()


def test_published_hours_count_counts_only_real_publishes():
    wm = _wm()
    for hour in (14, 15, 16):
        wm.record(node_id=NODE, season=74, hour=hour, hour_root="ab" * 32,
                  tx_id="0x" + "cd" * 32)
    assert wm.published_hours_count(NODE, 74) == 3
    # Scoped correctly — not leaking across seasons or nodes.
    assert wm.published_hours_count(NODE, 73) == 0
    assert wm.published_hours_count("F" * 32, 74) == 0
    wm.close()


def test_the_old_formula_and_the_new_one_disagree_exactly_as_reported():
    """Pin the actual defect, so a revert is loud."""
    wm = _wm()
    hour = 14
    old = hour + 1                                   # what shipped: 15
    new = wm.published_hours_count(NODE, 74) + 1     # measured: 1
    assert old == 15
    assert new == 1
    assert old != new, "the hour index is not an uptime count"
    wm.close()


def test_hour_index_never_leaks_into_the_count():
    """A single publish is one hour, whatever o'clock it happened to be."""
    wm = _wm()
    for hour in (0, 23):
        w = _wm()
        w.record(node_id=NODE, season=74, hour=hour, hour_root="ab" * 32,
                 tx_id="0x" + "cd" * 32)
        assert w.published_hours_count(NODE, 74) == 1, (
            f"publishing hour {hour} must count as 1 hour, not {hour + 1}"
        )
        w.close()
    wm.close()


# --- what harvest puts in the record ---------------------------------------
class _Oracle:
    def __init__(self, rows): self._rows = rows
    def get_readings(self, node_id, **kw): return self._rows


def _harvest(**kw):
    ch = schedule.ClosedHour(season=74, hour=14,
                             start_ms=1786197600000, end_ms=1786201200000,
                             start_utc=None)
    reading = {"node_id": NODE, "ts_ms": 1786200145000, "block_anchor": "0" * 16,
               "metrics": {"temperature_mc": 23063}, "sig": "ab" * 64}
    rows = [{"payload": {"device_reading": reading}}]
    return publish.harvest_closed_hour_batches(
        _Oracle(rows),
        [{"node_id": NODE, "sensors": ["ds18b20"], "device_pubkey": "02" + "cd" * 32,
          "fw_version": "0.6.0", "registered_at": "2026-08-08T02:47:12"}],
        [ch],
        already_published=lambda *_: False,
        current_season=74,
        **kw,
    )


def test_unmeasurable_claims_default_to_honest_values():
    # No measurement supplied -> claim nothing, rather than guess.
    batches, _ = _harvest()
    assert batches, "expected one batch"
    assert batches[0].running_hours_online == 0
    assert batches[0].last_sealed_season is None


def test_measured_values_are_used_when_available():
    batches, _ = _harvest(
        published_hours=lambda n, s: 4,
        sealed_season=lambda n, s: 73,
    )
    assert batches[0].running_hours_online == 5      # 4 already + this one
    assert batches[0].last_sealed_season == 73


def test_an_unsealed_season_is_reported_as_unsealed():
    # The exact live case: no attest record anywhere for this node.
    batches, _ = _harvest(
        published_hours=lambda n, s: 0,
        sealed_season=lambda n, s: None,
    )
    assert batches[0].running_hours_online == 1
    assert batches[0].last_sealed_season is None, (
        "with no attest record in the store, no season may be claimed as sealed"
    )
