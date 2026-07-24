# SPDX-License-Identifier: Apache-2.0
"""FixedWindowLimiter: correctness + bounded memory (key eviction)."""
from __future__ import annotations

from oracle.app.ratelimit import FixedWindowLimiter


def _clock():
    t = {"v": 0.0}
    return t, (lambda: t["v"])


def test_allows_up_to_limit_then_blocks():
    t, clk = _clock()
    lim = FixedWindowLimiter(limit=3, window_s=60, clock=clk)
    assert [lim.allow("ip") for _ in range(4)] == [True, True, True, False]


def test_window_resets_after_expiry():
    t, clk = _clock()
    lim = FixedWindowLimiter(limit=2, window_s=60, clock=clk)
    assert lim.allow("ip") and lim.allow("ip")
    assert lim.allow("ip") is False
    t["v"] = 61.0  # window elapsed
    assert lim.allow("ip") is True


def test_limit_zero_disables():
    lim = FixedWindowLimiter(limit=0)
    assert all(lim.allow("ip") for _ in range(100))


def test_evicts_vanished_keys_once_over_threshold():
    t, clk = _clock()
    lim = FixedWindowLimiter(limit=5, window_s=60, clock=clk, sweep_threshold=10)
    for i in range(20):
        lim.allow(f"ip{i}")
    assert lim.size() == 20  # all tracked, none aged out yet
    # Advance past the window so every existing key's hits age out, then a new
    # request (map already > threshold) triggers a sweep that reclaims them.
    t["v"] = 1000.0
    lim.allow("ip-new")
    assert lim.size() == 1  # 20 vanished keys reclaimed; only the active one left


def test_sweep_keeps_active_keys():
    t, clk = _clock()
    lim = FixedWindowLimiter(limit=5, window_s=60, clock=clk, sweep_threshold=5)
    # 10 stale keys hit at t=0; two active keys hit at t=30.
    for i in range(10):
        lim.allow(f"stale{i}")
    t["v"] = 30.0
    lim.allow("active-a")
    lim.allow("active-b")
    # At t=61 (window 60): stale hits (t=0) have aged out; active hits (t=30)
    # are still within the window. A new request triggers a sweep.
    t["v"] = 61.0
    lim.allow("trigger")
    assert "active-a" in lim._hits and "active-b" in lim._hits
    assert not any(k.startswith("stale") for k in lim._hits)
