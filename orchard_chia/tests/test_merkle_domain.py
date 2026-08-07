# SPDX-License-Identifier: Apache-2.0
"""Assert Domain separation constants for Merkle (SPEC §5)."""
from orchard_chia.datalayer import merkle

def test_leaf_prefix_differs_from_node():
    a = merkle.leaf_hash(b"x")
    b = merkle.node_hash(a, a)
    assert a != b
    assert len(a) == 32
