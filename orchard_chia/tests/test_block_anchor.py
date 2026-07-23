# SPDX-License-Identifier: Apache-2.0
"""Anti-backdate block-anchor kernel (pure)."""
from __future__ import annotations

from orchard_chia.datalayer import block_anchor as ba

# A reading at ts_ms; anchor is the first 16 hex of a header hash.
TS_MS = 1_749_480_000_000  # seconds: 1_749_480_000
ANCHOR = "a1b2c3d4e5f60718"


def _block(header_hash, timestamp):
    return {"header_hash": header_hash, "height": 100, "timestamp": timestamp}


def test_match_prefix_and_timestamp_before():
    b = _block("0x" + ANCHOR + "cc" * 24, TS_MS // 1000 - 5)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is True


def test_no_match_wrong_prefix():
    b = _block("0x" + "ffff" + "cc" * 30, TS_MS // 1000 - 5)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is False


def test_no_match_timestamp_after_reading():
    # Block is newer than the reading — cannot bound it from below.
    b = _block("0x" + ANCHOR + "cc" * 24, TS_MS // 1000 + 60)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is False


def test_non_transaction_block_has_no_timestamp():
    b = _block("0x" + ANCHOR + "cc" * 24, None)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is False


def test_bool_timestamp_rejected():
    # bool is an int subclass — must not be treated as a timestamp.
    b = _block("0x" + ANCHOR + "cc" * 24, True)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is False


def test_header_hash_without_0x_prefix_also_matches():
    b = _block(ANCHOR + "cc" * 24, TS_MS // 1000 - 5)
    assert ba.anchor_matches(b, ANCHOR, TS_MS) is True


def test_empty_anchor_never_matches():
    b = _block("0x" + ANCHOR + "cc" * 24, TS_MS // 1000 - 5)
    assert ba.anchor_matches(b, "", TS_MS) is False


def test_find_anchor_block_scans_records():
    records = [
        _block("0x" + "0000" + "11" * 30, TS_MS // 1000 - 100),
        _block("0x" + ANCHOR + "cc" * 24, TS_MS // 1000 - 5),
    ]
    hit = ba.find_anchor_block(records, ANCHOR, TS_MS)
    assert hit is not None and hit["timestamp"] == TS_MS // 1000 - 5

    assert ba.find_anchor_block(records[:1], ANCHOR, TS_MS) is None
