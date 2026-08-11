# SPDX-License-Identifier: Apache-2.0
"""get_keys must read the whole store, and must never call an unread store empty.

Measured against the live store on 2026-08-09: ``get_keys`` returned 0 keys
while roughly 200 were present. The ``page`` parameter is 0-indexed and the
client asked for page 1, which a node answers with an empty list rather than an
error.

Nothing crashed. The payout reader found no attestations and reported a
successful run having paid nobody; the verifier found nothing to check and had
nothing to complain about. An empty answer to the wrong question is the most
expensive kind of bug in a system whose job is to publish evidence.

Two properties are pinned here:
  * the whole key set is read, under either indexing convention
  * "I read nothing" and "there is nothing" are never the same answer
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc


class FakeNode:
    """A DataLayer node with a chosen page-index convention."""

    def __init__(self, pages: list[list[str]], *, base: int = 0,
                 reject_other_base: bool = True):
        self.pages = pages
        self.base = base
        self.reject_other_base = reject_other_base
        self.asked: list[int | None] = []

    def post(self, route, body, timeout=60):
        assert route == "get_keys"
        page = body.get("page")
        self.asked.append(page)
        if page is None:
            return {"keys": [k for p in self.pages for k in p]}
        idx = page - self.base
        if idx < 0 or idx >= len(self.pages):
            if self.reject_other_base:
                raise ChiaRpcError(f"invalid page {page}")
            return {"keys": [], "total_pages": len(self.pages)}
        return {"keys": self.pages[idx], "total_pages": len(self.pages)}


def rpc_for(node) -> DataLayerRpc:
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = node.post          # type: ignore[method-assign]
    return r


# --- the live defect --------------------------------------------------------

def test_a_zero_indexed_node_is_read_completely():
    """The exact live case: one page of keys, 0-indexed."""
    node = FakeNode([["6b31", "6b32", "6b33"]], base=0)
    assert rpc_for(node).get_keys("store") == ["6b31", "6b32", "6b33"]


def test_a_zero_indexed_node_that_answers_page_one_with_silence():
    """A node that returns an empty list instead of erroring on a bad page —
    the shape that made this invisible for so long."""
    node = FakeNode([["a1", "a2"]], base=0, reject_other_base=False)
    assert rpc_for(node).get_keys("store") == ["a1", "a2"]


def test_multiple_pages_are_concatenated_zero_indexed():
    node = FakeNode([["a"], ["b"], ["c"]], base=0)
    assert rpc_for(node).get_keys("store") == ["a", "b", "c"]


def test_a_one_indexed_node_still_works():
    """Probing means a node that changes convention costs one wasted request,
    not a silently empty dataset."""
    node = FakeNode([["a"], ["b"]], base=1)
    assert rpc_for(node).get_keys("store") == ["a", "b"]


def test_a_genuinely_empty_store_returns_empty():
    """One page holding nothing IS an empty store. The probe already tried
    both conventions, so there is nothing left for the guard to catch."""
    node = FakeNode([[]], base=0)
    assert rpc_for(node).get_keys("store") == []


# --- an unread store is not an empty one ------------------------------------

def test_no_keys_while_the_node_reports_pages_is_an_error():
    """The finding this exists to stop: a reader that treats a failed read as
    evidence of absence, and exits 0 having paid nobody."""

    class Contradictory:
        def post(self, route, body, timeout=60):
            return {"keys": [], "total_pages": 5}

    with pytest.raises(ChiaRpcError, match="cannot have an empty first page"):
        rpc_for(Contradictory()).get_keys("store")


def test_the_error_names_the_real_cause():
    class Contradictory:
        def post(self, route, body, timeout=60):
            return {"keys": [], "total_pages": 3}

    with pytest.raises(ChiaRpcError, match="page index convention"):
        rpc_for(Contradictory()).get_keys("store")


def test_an_unreachable_store_still_soft_fails_for_scanners():
    """get_keys keeps its documented soft-fail for the scanner path — but see
    the strict variant below, which is what any reader treating emptiness as
    evidence must use."""

    class Dead:
        def post(self, route, body, timeout=60):
            raise ChiaRpcError("connection refused")

    assert rpc_for(Dead()).get_keys("store") == []


def test_strict_propagates_instead_of_soft_failing():
    class Dead:
        def post(self, route, body, timeout=60):
            raise ChiaRpcError("connection refused")

    with pytest.raises(ChiaRpcError):
        rpc_for(Dead()).get_keys_strict("store")


def test_strict_reads_the_whole_store_too():
    node = FakeNode([["a"], ["b"], ["c"]], base=0)
    assert rpc_for(node).get_keys_strict("store") == ["a", "b", "c"]


def test_a_mid_pagination_failure_is_not_a_truncated_success():
    class HalfDead:
        def __init__(self):
            self.n = 0

        def post(self, route, body, timeout=60):
            self.n += 1
            if body.get("page") == 0:
                return {"keys": ["a"], "total_pages": 3}
            raise ChiaRpcError("node fell over on page 2")

    with pytest.raises(ChiaRpcError):
        rpc_for(HalfDead()).get_keys("store")


def test_a_node_without_pagination_info_is_read_in_one_shot():
    class OneShot:
        def post(self, route, body, timeout=60):
            return {"keys": ["a", "b"]}

    assert rpc_for(OneShot()).get_keys("store") == ["a", "b"]


def test_a_node_rejecting_the_page_param_falls_back_to_unpaginated():
    class NoPaging:
        def post(self, route, body, timeout=60):
            if "page" in body:
                raise ChiaRpcError("unknown parameter: page")
            return {"keys": ["a", "b", "c"]}

    assert rpc_for(NoPaging()).get_keys("store") == ["a", "b", "c"]


def test_prefixed_keys_are_normalized_at_the_boundary():
    """The live daemon returns 0x-prefixed keys. Hour discovery fed them to
    bytes.fromhex unstripped and silently found no hours — attest then
    refused season 76 as a placeholder while its readings sat on chain. The
    third appearance of the 0x bug class; the boundary now ends it."""
    node = FakeNode([["0x6b31", "6b32"]], base=0)
    assert rpc_for(node).get_keys("store") == ["6b31", "6b32"]


def test_strict_normalizes_too():
    node = FakeNode([["0xaa", "0xbb"]], base=0)
    assert rpc_for(node).get_keys_strict("store") == ["aa", "bb"]
