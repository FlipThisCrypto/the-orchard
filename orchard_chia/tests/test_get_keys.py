# SPDX-License-Identifier: Apache-2.0
"""DataLayerRpc.get_keys pagination — no silent truncation on large stores."""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc
from orchard_chia.datalayer.retry import RetryPolicy


def _rpc(fake_post) -> DataLayerRpc:
    dl = DataLayerRpc("127.0.0.1", 1, "c", "k", retry_policy=RetryPolicy(max_attempts=1))
    dl._post = fake_post  # type: ignore[assignment]
    return dl


def test_get_keys_follows_total_pages():
    seen = []

    def fake_post(route, body):
        seen.append(body.get("page"))
        page = body.get("page")
        pages = {
            1: {"success": True, "keys": ["aa", "bb"], "total_pages": 3},
            2: {"success": True, "keys": ["cc"], "total_pages": 3},
            3: {"success": True, "keys": ["dd"], "total_pages": 3},
        }
        if page not in pages:
            # A 1-indexed node REJECTS page 0; it does not KeyError. Modelling
            # that faithfully is the difference between this test proving the
            # client copes with either convention and it merely proving the
            # client asks the number this fake happens to expect.
            raise ChiaRpcError(f"invalid page {page}")
        return pages[page]

    assert _rpc(fake_post).get_keys("S") == ["aa", "bb", "cc", "dd"]
    assert seen == [0, 1, 2, 3], "page 0 is probed first, then the 1-indexed run"


def test_get_keys_single_response_no_total_pages():
    def fake_post(route, body):
        return {"success": True, "keys": ["aa", "bb"]}  # no total_pages

    assert _rpc(fake_post).get_keys("S") == ["aa", "bb"]


def test_get_keys_first_page_unreachable_soft_empty():
    def fake_post(route, body):
        raise ChiaRpcError("store unreachable")

    assert _rpc(fake_post).get_keys("S") == []


def test_get_keys_falls_back_when_page_param_rejected():
    def fake_post(route, body):
        if "page" in body:
            raise ChiaRpcError("unexpected keyword 'page'")
        return {"success": True, "keys": ["aa"]}

    assert _rpc(fake_post).get_keys("S") == ["aa"]


def test_get_keys_mid_pagination_failure_raises():
    # Truncation must be loud, never a silently-short list.
    def fake_post(route, body):
        if body.get("page") == 1:
            return {"success": True, "keys": ["aa"], "total_pages": 2}
        raise ChiaRpcError("page 2 down")

    with pytest.raises(ChiaRpcError):
        _rpc(fake_post).get_keys("S")
