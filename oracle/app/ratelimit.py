# SPDX-License-Identifier: Apache-2.0
"""Tiny dependency-free fixed-window rate limiter for the oracle.

Used by ``main.py``'s middleware to bound remote LAN callers on the
sensitive endpoints (/auth/*, /readings). Loopback callers — the
operator's own dashboard and the local DataLayer writer — are exempt
(the middleware checks that before consulting a limiter), so this only
throttles the network-reachable surface.

Deterministic and injectable-clock so it can be unit-tested without
sleeping.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable


class FixedWindowLimiter:
    """Allow up to ``limit`` events per ``window_s`` seconds, per key."""

    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sweep_threshold: int = 1024,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        # Once the per-key map exceeds this many entries, sweep out keys whose
        # window has fully aged out. Without this the map grows one entry per
        # distinct client key FOREVER (empty deques were never removed) — an
        # unbounded-memory vector under high-cardinality or IP-rotating abuse,
        # especially when the oracle is exposed via a public tunnel.
        self._sweep_threshold = max(0, int(sweep_threshold))
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an event for ``key``; return False if it exceeds the
        limit within the current window."""
        if self.limit <= 0:
            return True  # disabled
        now = self._clock()
        if self._sweep_threshold and len(self._hits) > self._sweep_threshold:
            self._sweep(now)
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        cutoff = now - self.window_s
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Drop keys whose entire window has aged out (bounds memory)."""
        cutoff = now - self.window_s
        dead: list[str] = []
        for k, dq in self._hits.items():
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                dead.append(k)
        for k in dead:
            del self._hits[k]

    def size(self) -> int:
        """Number of tracked keys — for tests / capacity observability."""
        return len(self._hits)

    def reset(self) -> None:
        self._hits.clear()
