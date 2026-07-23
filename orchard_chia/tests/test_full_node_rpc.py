# SPDX-License-Identifier: Apache-2.0
"""FullNodeRpc block-lookup request/response shapes (anti-backdate path)."""
from __future__ import annotations

from orchard_chia.datalayer.rpc import FullNodeRpc


def _capturing(responses):
    fn = FullNodeRpc("127.0.0.1", 8555, "c", "k")
    seen = {}

    def fake_post(route, body):
        seen["route"] = route
        seen["body"] = body
        return responses.get(route, {"success": True})

    fn._post = fake_post  # type: ignore[assignment]
    return fn, seen


def test_get_block_record_by_height_body_and_extract():
    br = {"header_hash": "0xab", "height": 100, "timestamp": 1700000000}
    fn, seen = _capturing({"get_block_record_by_height": {"block_record": br}})
    out = fn.get_block_record_by_height(100)
    assert seen["route"] == "get_block_record_by_height"
    assert seen["body"] == {"height": 100}
    assert out == br


def test_get_block_record_by_header_hash():
    br = {"header_hash": "0xcd", "timestamp": None}  # non-tx block: no timestamp
    fn, seen = _capturing({"get_block_record": {"block_record": br}})
    out = fn.get_block_record("0xcd")
    assert seen["body"] == {"header_hash": "0xcd"}
    assert out == br


def test_get_block_records_range_and_default_empty():
    fn, seen = _capturing({"get_block_records": {"block_records": [{"height": 5}]}})
    out = fn.get_block_records(5, 10)
    assert seen["body"] == {"start": 5, "end": 10}
    assert out == [{"height": 5}]

    # Missing/None block_records -> [] (not None), so callers can iterate safely.
    fn2, _ = _capturing({"get_block_records": {"success": True}})
    assert fn2.get_block_records(0, 1) == []


def test_peak_height_reads_blockchain_state():
    fn, _ = _capturing({
        "get_blockchain_state": {
            "blockchain_state": {"peak": {"height": 8794728}}
        }
    })
    assert fn.peak_height() == 8794728
