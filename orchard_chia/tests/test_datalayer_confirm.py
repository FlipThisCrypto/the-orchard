# SPDX-License-Identifier: Apache-2.0
"""Tests for the Season-writer root-confirmation wait (T16 / ADR-0010)."""
from __future__ import annotations

from orchard_chia.datalayer.main import wait_for_root_confirmation
from orchard_chia.datalayer.rpc import ChiaRpcError


class _FakeDL:
    """Returns successive get_root() results; an Exception entry is raised."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def get_root(self, _store_id):
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


class _Clock:
    """Deterministic clock: each _sleep(n) advances time by n; no real waiting."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, n):
        self.t += n


def _wait(dl, **kw):
    clk = _Clock()
    return wait_for_root_confirmation(dl, "store", _sleep=clk.sleep, _clock=clk.now, **kw)


def test_confirms_after_a_couple_polls():
    dl = _FakeDL([{"confirmed": False}, {"confirmed": False}, {"confirmed": True}])
    assert _wait(dl, timeout_s=60, poll_s=10) is True


def test_times_out_when_never_confirmed():
    dl = _FakeDL([{"confirmed": False}])  # stuck unconfirmed forever
    assert _wait(dl, timeout_s=30, poll_s=10) is False


def test_transient_rpc_error_is_tolerated_then_confirms():
    dl = _FakeDL([ChiaRpcError("blip"), {"confirmed": True}])
    assert _wait(dl, timeout_s=60, poll_s=10) is True


def test_missing_confirmed_field_is_treated_as_unconfirmed():
    dl = _FakeDL([{}, {"confirmed": True}])
    assert _wait(dl, timeout_s=60, poll_s=10) is True
    # never confirms if the field never shows up
    assert _wait(_FakeDL([{}]), timeout_s=20, poll_s=10) is False
