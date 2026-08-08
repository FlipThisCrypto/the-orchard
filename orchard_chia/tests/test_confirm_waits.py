# SPDX-License-Identifier: Apache-2.0
"""A DataLayer write is not readable the instant batch_update returns.

These tests pin the distinction that the first real publish to the Orchard's
store (2026-08-08, tx 0xa3577121…) exposed: the write was accepted, sat in the
mempool, and the immediate read-back reported ``missing=['meta:schema']``. The
root advanced and confirmed a minute later and the value was exactly right.

So "not in a block yet" (pending, harmless, wait) must never be reported as
"the values are wrong" (failed, real). It matters beyond tidiness: the old
advice on that path was "re-run to converge", and re-running while the
transaction is pending submits the same write a second time and pays the fee
twice.
"""
from __future__ import annotations

from orchard_chia.datalayer import confirm

ROOT_BEFORE = "0x436a5e4674b401dcd7f85a3b19accfbc7ccbb9d174d3be4de7bd21a5fcb65b4e"
ROOT_AFTER = "0x5ccff1fec262477212fffbc9c2061a568e1e00232a0c85a16a635ae7d3ee97e3"
KEY = "6d6574613a736368656d61"      # "meta:schema"
VAL = "7b7d"


class FakeRpc:
    """A store whose root moves only after ``blocks_until_confirmed`` polls."""

    def __init__(self, blocks_until_confirmed: int, *, value_after=VAL):
        self.polls = 0
        self.blocks = blocks_until_confirmed
        self.value_after = value_after

    def get_root(self, store_id):
        self.polls += 1
        if self.polls > self.blocks:
            return {"hash": ROOT_AFTER, "confirmed": True}
        return {"hash": ROOT_BEFORE, "confirmed": True}   # pre-write root, already confirmed

    def get_value(self, store_id, key_hex):
        return self.value_after if self.polls > self.blocks else None


def _run(rpc, **kw):
    slept: list[float] = []
    clock = {"t": 0.0}

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    return (
        confirm.confirm_after_write(
            rpc, "store", [(KEY, VAL)],
            root_before=ROOT_BEFORE, sleep=sleep, monotonic=lambda: clock["t"], **kw
        ),
        slept,
    )


def test_the_exact_scenario_that_misfired_now_succeeds():
    # Accepted, one block later it confirms. Previously: "confirm failed".
    res, slept = _run(FakeRpc(blocks_until_confirmed=2))
    assert res.ok is True, res.detail
    assert res.pending is False
    assert res.missing == [] and res.mismatched == []
    assert slept, "it must actually wait rather than judging immediately"


def test_a_pre_write_root_that_is_already_confirmed_does_not_count():
    # The trap: the OLD root is confirmed too. Polling for `confirmed` alone
    # would return instantly and prove nothing about our write.
    rpc = FakeRpc(blocks_until_confirmed=3)
    res, _ = _run(rpc)
    assert res.ok is True
    assert rpc.polls > 1, "returned on the pre-write root — it proved nothing"


def test_never_confirming_is_pending_not_failed():
    # The honest outcome when a transaction genuinely stalls: not a failure,
    # and explicitly not something to fix by re-running.
    res, _ = _run(FakeRpc(blocks_until_confirmed=10_000), timeout_s=60, poll_s=15)
    assert res.pending is True
    assert res.ok is False
    assert res.missing == [] and res.mismatched == []
    assert "not yet confirmed" in res.detail


def test_a_real_failure_is_still_a_real_failure():
    # Root moved and confirmed, but the value is absent. That IS broken, and
    # must not be softened into "pending".
    rpc = FakeRpc(blocks_until_confirmed=1, value_after=None)
    res, _ = _run(rpc)
    assert res.ok is False
    assert res.pending is False, "a missing value after confirmation is not pending"
    assert res.missing == ["meta:schema"]


def test_a_wrong_value_is_a_failure_not_pending():
    rpc = FakeRpc(blocks_until_confirmed=1, value_after="deadbeef")
    res, _ = _run(rpc)
    assert res.ok is False and res.pending is False
    assert res.mismatched == ["meta:schema"]


def test_waiting_is_bounded():
    slept_total = []
    clock = {"t": 0.0}

    def sleep(s):
        slept_total.append(s)
        clock["t"] += s

    confirm.confirm_after_write(
        FakeRpc(10_000), "store", [(KEY, VAL)], root_before=ROOT_BEFORE,
        timeout_s=90, poll_s=15, sleep=sleep, monotonic=lambda: clock["t"],
    )
    assert sum(slept_total) <= 90 + 1e-6, "the wait must respect its deadline"


def test_an_rpc_blip_does_not_read_as_a_failed_write():
    class Flaky(FakeRpc):
        def get_root(self, store_id):
            self.polls += 1
            if self.polls == 1:
                raise RuntimeError("connection reset")
            return {"hash": ROOT_AFTER, "confirmed": True}

    res, _ = _run(Flaky(blocks_until_confirmed=0))
    assert res.ok is True, "a transient get_root error must not condemn the write"


def test_nothing_to_confirm_short_circuits():
    res = confirm.confirm_after_write(FakeRpc(0), "store", [], root_before=ROOT_BEFORE)
    assert res.ok is True and res.pending is False and res.checked == 0
