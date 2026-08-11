# SPDX-License-Identifier: Apache-2.0
"""No baseline root, no write; no baseline, no confirmation.

The pre-write get_root could blip, leaving root_before=None — and confirm's
"has the root moved?" check treated None as already-moved, so it passed
instantly against the OLD confirmed root. A run that may never have landed
reported a clean success and advanced the watermark on values a previous
write put there. Those hours became "already published" forever, unchecked.

Now: a write is refused when the baseline cannot be read (one lost scheduler
tick beats a false confirm), and a missing baseline can never satisfy the
movement check.
"""
from __future__ import annotations

from orchard_chia.datalayer import confirm


class Root:
    def __init__(self, answers):
        self.answers = list(answers)

    def get_root(self, store_id):
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def test_a_missing_baseline_never_counts_as_movement():
    """The exact false-confirm: old root confirmed, baseline lost."""
    rpc = Root([{"hash": "0xOLD", "confirmed": True}])
    ok, why = confirm.wait_for_root(
        rpc, "store", root_before=None, timeout_s=0.05, poll_s=0.01)
    assert ok is False
    assert "unconfirmed" in why


def test_a_real_baseline_and_a_moved_root_confirms():
    rpc = Root([{"hash": "0xNEW", "confirmed": True}])
    ok, why = confirm.wait_for_root(
        rpc, "store", root_before="0xOLD", timeout_s=0.05, poll_s=0.01)
    assert ok is True and "advanced" in why


def test_an_unmoved_root_does_not_confirm():
    rpc = Root([{"hash": "0xOLD", "confirmed": True}])
    ok, _ = confirm.wait_for_root(
        rpc, "store", root_before="0xOLD", timeout_s=0.05, poll_s=0.01)
    assert ok is False


def test_publish_refuses_to_write_without_a_baseline():
    """Source-level pin: the fallback to root_before=None on the write path is
    gone, replaced by a refusal that names the reason."""
    import inspect
    from orchard_chia.datalayer import publish, main as attest_main
    for mod in (publish, attest_main):
        src = inspect.getsource(mod)
        assert "Refusing to submit" in src
        assert "root_before = None          # unknown" not in src
