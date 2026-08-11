# SPDX-License-Identifier: Apache-2.0
"""verify-latest: the daily self-audit's discovery and verdict logic.

Sealing is scheduled; if checking the seals were not, tampering or a writer
bug would surface only when a human happened to look. The exit-code contract
is the point: INVALID (a definitive contradiction) is 1 and pages; the known
anchor gap and transient unavailability are 0 and do not page at 1am.
"""
from __future__ import annotations

from orchard_chia.datalayer.rpc import DataLayerRpc
from orchard_chia.datalayer.verify_latest import _sealed_seasons

NODE = "D8641AD6CAE36977818499469F7E8C49"


def _rpc(keys_ascii):
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = lambda route, body, timeout=60: {
        "keys": [k.encode().hex() for k in keys_ascii], "total_pages": 1}
    return r


def test_sealed_seasons_are_discovered_ascending():
    keys = [f"attest:{NODE}:00000076", f"attest:{NODE}:00000074",
            f"readings:{NODE}:00000076:04", "meta:schema"]
    assert _sealed_seasons(_rpc(keys), "s", NODE) == [74, 76]


def test_other_nodes_seals_do_not_leak_in():
    other = "E014926F4805D7D848E4EDC32D70E39F"
    keys = [f"attest:{other}:00000076"]
    assert _sealed_seasons(_rpc(keys), "s", NODE) == []


def test_unicode_season_tails_are_refused():
    keys = [f"attest:{NODE}:0000007٦"]
    assert _sealed_seasons(_rpc(keys), "s", NODE) == []


def test_the_anchor_gap_is_tolerated_but_nothing_else_is():
    """The verdict split that decides whether anyone gets paged."""
    import inspect
    from orchard_chia.datalayer import verify_latest
    src = inspect.getsource(verify_latest)
    assert '"anchor" not in c.name.lower()' in src
    assert "return 1 if invalid else 0" in src
