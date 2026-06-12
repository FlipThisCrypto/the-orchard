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
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an event for ``key``; return False if it exceeds the
        limit within the current window."""
        if self.limit <= 0:
            return True  # disabled
        now = self._clock()
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

    def reset(self) -> None:
        self._hits.clear()
