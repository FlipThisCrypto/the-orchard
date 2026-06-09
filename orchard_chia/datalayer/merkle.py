# SPDX-License-Identifier: Apache-2.0
"""Domain-separated SHA-256 Merkle tree for Orchard reading commitments.

Pinned by ``docs/datalayer/SPEC.md`` §5. Pure functions, no I/O.

    leaf_hash(data) = sha256( 0x00 || data )
    node_hash(l, r) = sha256( 0x01 || l || r )

Rules (must match the firmware + JS verifiers byte-for-byte):

- **Domain separation:** the 0x00 / 0x01 prefixes stop a leaf from ever being
  reinterpreted as an internal node (second-preimage defense).
- **Odd level:** the last node is *promoted* (carried up unchanged), never
  duplicated.
- **Empty set:** the root is the ``EMPTY_ROOT`` sentinel ``sha256(0x00)``, never
  an empty/zero value.
- **Single leaf:** ``merkle_root([x]) == x`` — a one-element tree is its own
  root. (This is what lets a one-hour Season's root equal that hour's root, so a
  reading→Season proof is just the hour proof concatenated with the season
  proof — see SPEC §5.)

Hashes are 32 raw bytes internally; callers hex-encode at the edges.
"""
from __future__ import annotations

import hashlib

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

# Root of the empty set. Distinct from any real leaf because a real leaf is
# sha256(0x00 || data) with non-empty data, while this is sha256(0x00).
EMPTY_ROOT = hashlib.sha256(LEAF_PREFIX).digest()


def leaf_hash(data: bytes) -> bytes:
    """Hash of a leaf's raw bytes, domain-separated with 0x00."""
    return hashlib.sha256(LEAF_PREFIX + bytes(data)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """Hash of two child nodes, domain-separated with 0x01."""
    return hashlib.sha256(NODE_PREFIX + bytes(left) + bytes(right)).digest()


def merkle_root(leaves: list[bytes]) -> bytes:
    """Root over a list of 32-byte leaves. Promote-last on odd levels."""
    if not leaves:
        return EMPTY_ROOT
    level = [bytes(x) for x in leaves]
    while len(level) > 1:
        nxt: list[bytes] = []
        i = 0
        while i < len(level):
            if i + 1 < len(level):
                nxt.append(node_hash(level[i], level[i + 1]))
                i += 2
            else:
                nxt.append(level[i])  # promote the odd tail unchanged
                i += 1
        level = nxt
    return level[0]


def merkle_proof(leaves: list[bytes], index: int) -> list[tuple[str, str]]:
    """Audit path for ``leaves[index]``.

    Returns a list of ``(side, sibling_hex)`` steps, bottom-up, where ``side``
    is the sibling's position: ``"L"`` = sibling on the left, ``"R"`` = sibling
    on the right. A promoted (odd-tail) node contributes no step. Feed the
    result to :func:`verify_proof`.
    """
    if not (0 <= index < len(leaves)):
        raise IndexError("leaf index out of range")
    proof: list[tuple[str, str]] = []
    level = [bytes(x) for x in leaves]
    idx = index
    while len(level) > 1:
        nxt: list[bytes] = []
        new_idx: int | None = None
        i = 0
        while i < len(level):
            if i + 1 < len(level):
                pos = len(nxt)
                if idx == i:
                    proof.append(("R", level[i + 1].hex()))
                    new_idx = pos
                elif idx == i + 1:
                    proof.append(("L", level[i].hex()))
                    new_idx = pos
                nxt.append(node_hash(level[i], level[i + 1]))
                i += 2
            else:
                if idx == i:
                    new_idx = len(nxt)
                nxt.append(level[i])  # promote
                i += 1
        assert new_idx is not None  # idx is always within the level
        idx = new_idx
        level = nxt
    return proof


def verify_proof(leaf: bytes, proof: list[tuple[str, str]], root: bytes) -> bool:
    """Fold ``leaf`` up its audit ``proof`` and check it reaches ``root``."""
    h = bytes(leaf)
    for side, sib_hex in proof:
        sib = bytes.fromhex(sib_hex)
        h = node_hash(h, sib) if side == "R" else node_hash(sib, h)
    return h == bytes(root)
