# SPDX-License-Identifier: Apache-2.0
"""Process-local counters for oracle observability (tester / ops).

Counts only — never stores payloads, signatures, wallets, or secrets.
Intended for /health so operators can see replay pressure and abuse
without scraping logs. Counters reset on process restart (acceptable
for PoC; Prometheus can replace this later without changing routes).
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_counts: dict[str, int] = {}


def incr(name: str, n: int = 1) -> None:
    if n <= 0:
        return
    with _lock:
        _counts[name] = _counts.get(name, 0) + n


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(sorted(_counts.items()))


def reset_for_tests() -> None:
    with _lock:
        _counts.clear()


def as_public_dict() -> dict[str, Any]:
    """Stable shape for /health — always include known keys (0 if never hit)."""
    keys = (
        "readings_accepted",
        "readings_duplicate",
        "readings_rejected_body_too_large",
        "readings_rejected_bad_sig",
        "readings_rejected_unknown_node",
        "readings_rejected_replay_seq",
        "readings_rejected_missing_seq",
        "readings_rejected_stale_ts",
        "readings_rejected_future_ts",
        "readings_rejected_bad_json",
        "rate_limited",
    )
    snap = snapshot()
    return {k: int(snap.get(k, 0)) for k in keys}
