# SPDX-License-Identifier: Apache-2.0
"""In-process request observability for the oracle.

Gives an operator the signals needed to investigate incidents without any
external infrastructure:

- a **request id** per request (echoed in ``X-Request-ID`` and every log line
  for that request, so logs can be correlated),
- **structured request/error logging**,
- **lightweight metrics** (totals, error count, per-route count/latency),
  exposed via the loopback-only ``/metrics`` endpoint.

Metrics are in-process and reset on restart — this is live operational signal,
not historical analytics.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import defaultdict

logger = logging.getLogger("orchard.oracle")


def new_request_id() -> str:
    """Short correlation id (16 hex chars)."""
    return secrets.token_hex(8)


# A path segment that looks like an id (node id hex, season, numeric pk).
_ID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8,}$")


def normalize_path(path: str) -> str:
    """Collapse dynamic path segments to ``:id`` so per-route metrics don't
    explode into unbounded cardinality (one bucket per node id / season)."""
    out = []
    for seg in path.split("/"):
        if seg.isdigit() or _ID_SEGMENT.match(seg):
            out.append(":id")
        else:
            out.append(seg)
    return "/".join(out)


def route_label(method: str, path: str) -> str:
    """Stable metrics/log label, e.g. ``GET /readings/:id``."""
    return f"{method} {normalize_path(path)}"


def _empty_route() -> dict:
    return {"count": 0, "errors": 0, "dur_ms_sum": 0.0, "dur_ms_max": 0.0}


class Metrics:
    """Thread-safe in-process request counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self.requests_total = 0
        self.errors_total = 0  # 5xx / unhandled
        self.by_status_class: dict[str, int] = defaultdict(int)
        self.by_route: dict[str, dict] = defaultdict(_empty_route)

    def record(self, route: str, status: int, duration_ms: float) -> None:
        with self._lock:
            self.requests_total += 1
            self.by_status_class[f"{status // 100}xx"] += 1
            if status >= 500:
                self.errors_total += 1
            r = self.by_route[route]
            r["count"] += 1
            if status >= 500:
                r["errors"] += 1
            r["dur_ms_sum"] += duration_ms
            if duration_ms > r["dur_ms_max"]:
                r["dur_ms_max"] = duration_ms

    def snapshot(self) -> dict:
        with self._lock:
            routes = {}
            for name, v in self.by_route.items():
                c = v["count"] or 1
                routes[name] = {
                    "count": v["count"],
                    "errors": v["errors"],
                    "avg_ms": round(v["dur_ms_sum"] / c, 2),
                    "max_ms": round(v["dur_ms_max"], 2),
                }
            return {
                "uptime_s": round(time.monotonic() - self._start, 1),
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "by_status_class": dict(self.by_status_class),
                "routes": routes,
            }

    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()
            self.requests_total = 0
            self.errors_total = 0
            self.by_status_class = defaultdict(int)
            self.by_route = defaultdict(_empty_route)


# Process-wide metrics registry.
METRICS = Metrics()
