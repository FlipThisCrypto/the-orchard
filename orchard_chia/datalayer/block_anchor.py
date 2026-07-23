# SPDX-License-Identifier: Apache-2.0
"""Anti-backdate block-anchor kernel (SPEC §4.2 / §7 check 4).

A reading embeds ``block_anchor`` — the first 16 hex chars (8 bytes) of a recent
Chia header hash. Because you cannot sign a block hash that does not yet exist,
matching that prefix to a real block whose ``timestamp ≤ ts_ms`` bounds the
reading's creation time **from below**: it was produced no earlier than that
block.

This module is the pure, offline-testable core of that check: given block
records (fetched by the caller from a full node) it decides whether one anchors
a reading. The *live orchestration* — deciding which height window to fetch so
the anchor block is in range — needs a synced full node and is layered on top
(still deferred; see SPEC §7). Kept pure so it carries no I/O.
"""
from __future__ import annotations

from typing import Any, Iterable


def _norm_hex(s: Any) -> str:
    """Lowercase hex with any ``0x`` prefix stripped. Chia serializes bytes32
    (e.g. header_hash) as ``0x…``; our anchor is raw hex — normalize both."""
    if not isinstance(s, str):
        return ""
    s = s.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return s


def anchor_matches(block_record: dict, anchor: str, ts_ms: int) -> bool:
    """True iff ``block_record``'s header hash starts with the ``anchor`` prefix
    AND the block carries a timestamp no later than ``ts_ms``.

    A non-transaction block has no ``timestamp`` (null) and therefore cannot
    bound the reading time — it never matches. The bound is
    ``block.timestamp (s) * 1000 ≤ ts_ms``.
    """
    a = _norm_hex(anchor)
    if not a:
        return False
    hh = _norm_hex(block_record.get("header_hash"))
    if not hh or not hh.startswith(a):
        return False
    ts = block_record.get("timestamp")
    if not isinstance(ts, int) or isinstance(ts, bool):
        return False  # non-transaction block: nothing to bound with
    return ts * 1000 <= int(ts_ms)


def find_anchor_block(
    block_records: Iterable[dict], anchor: str, ts_ms: int
) -> dict | None:
    """The first record that anchors the reading (``anchor_matches``), or None."""
    for r in block_records:
        if isinstance(r, dict) and anchor_matches(r, anchor, ts_ms):
            return r
    return None
