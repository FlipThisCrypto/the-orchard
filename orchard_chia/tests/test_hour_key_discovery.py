# SPDX-License-Identifier: Apache-2.0
"""Hour discovery accepts ASCII hours 00..23 and nothing else.

str.isdigit() accepts Unicode digits: "٠٧" (Arabic-Indic) passes and int()
parses it as 7. A hostile key readings:<NODE>:<SEASON>:٠٧ therefore shadowed
the real hour 07 — two distinct on-chain keys discovered as one hour, with the
completeness gate reporting a perfect match while verifying whichever bytes
loaded last.
"""
from __future__ import annotations

from orchard_chia.datalayer.fetch import _discover_hours
from orchard_chia.datalayer.rpc import DataLayerRpc

NODE = "AABBCCDDEEFF0011AABBCCDDEEFF0011"
PREFIX = f"readings:{NODE}:00000005:"


def _rpc(keys_ascii):
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = lambda route, body, timeout=60: {
        "keys": [k.encode().hex() for k in keys_ascii], "total_pages": 1}
    return r


def test_ascii_hours_are_discovered():
    assert _discover_hours(_rpc([PREFIX + "07", PREFIX + "13"]),
                           "store", NODE, 5) == [7, 13]


def test_unicode_digit_hours_are_refused():
    """The shadow key. It must not appear as hour 7."""
    assert _discover_hours(_rpc([PREFIX + "٠٧"]), "store", NODE, 5) == []


def test_a_shadowed_hour_is_discovered_exactly_once():
    got = _discover_hours(_rpc([PREFIX + "07", PREFIX + "٠٧"]),
                          "store", NODE, 5)
    assert got == [7], "the ASCII key wins; the Unicode twin is not an hour"


def test_out_of_range_hours_are_refused():
    assert _discover_hours(_rpc([PREFIX + "24", PREFIX + "99"]),
                           "store", NODE, 5) == []


def test_duplicate_ascii_keys_count_once():
    got = _discover_hours(_rpc([PREFIX + "07", PREFIX + "07"]),
                          "store", NODE, 5)
    assert got == [7]
