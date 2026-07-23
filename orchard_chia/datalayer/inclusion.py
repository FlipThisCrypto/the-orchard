# SPDX-License-Identifier: Apache-2.0
"""DataLayer inclusion / permanence checks (SPEC §7 check 1).

Fetches ``get_root`` + ``get_proof`` from the operator's DataLayer RPC and
summarizes whether the store reports a confirmed root and an inclusion
proof for the given keys.

Full CLVM-level proof verification against the singleton puzzle is a
later step (requires the on-chain coin state); this module is the
operator-facing RPC-level gate used by ``orchard-verify live``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .rpc import ChiaRpcError, DataLayerRpc


def key_clvm_hash(key_hex: str) -> str:
    """CLVM hash of a DataLayer key: ``sha256(0x01 || key_bytes)``, lowercase hex.

    ``get_proof`` returns only the CLVM hashes of keys/values (not the plaintext
    bytes), to keep proofs small. To decide whether a proof covers one of the
    keys we asked for, we recompute that key's CLVM hash and compare. The 0x01
    prefix is the CLVM atom-hash rule (chia-blockchain PR #16845;
    docs/datalayer/reference/CHIA_DATALAYER_RPC.md §4).
    """
    return hashlib.sha256(b"\x01" + bytes.fromhex(key_hex)).hexdigest()


@dataclass
class InclusionReport:
    ok: bool
    detail: str
    root_hash: str | None = None
    confirmed: bool | None = None
    keys_proven: int = 0


def check_inclusion(
    rpc: DataLayerRpc,
    store_id: str,
    key_hex_list: list[str],
) -> InclusionReport:
    """Return whether DataLayer reports a confirmed root + proof for keys."""
    if not key_hex_list:
        return InclusionReport(ok=False, detail="no keys to prove")

    try:
        root_resp = rpc.get_root(store_id)
    except ChiaRpcError as e:
        return InclusionReport(ok=False, detail=f"get_root failed: {e}")

    root_hash = (
        root_resp.get("hash")
        or root_resp.get("root_hash")
        or root_resp.get("root")
    )
    confirmed = root_resp.get("confirmed")
    if confirmed is None:
        # Some RPC versions omit confirmed; treat presence of a root as ok-ish.
        confirmed = bool(root_hash)

    try:
        proof_resp = rpc.get_proof(store_id, key_hex_list)
    except ChiaRpcError as e:
        return InclusionReport(
            ok=False,
            detail=f"get_proof failed: {e}",
            root_hash=root_hash if isinstance(root_hash, str) else None,
            confirmed=bool(confirmed) if confirmed is not None else None,
        )

    proven = _count_proven_keys(proof_resp, key_hex_list)
    root_s = root_hash if isinstance(root_hash, str) else None
    conf = bool(confirmed)

    if not root_s:
        return InclusionReport(
            ok=False,
            detail="get_root returned no hash",
            root_hash=None,
            confirmed=conf,
            keys_proven=proven,
        )
    if proven < len(key_hex_list):
        return InclusionReport(
            ok=False,
            detail=(
                f"proof covers {proven}/{len(key_hex_list)} key(s); "
                f"root={root_s[:16]}… confirmed={conf}"
            ),
            root_hash=root_s,
            confirmed=conf,
            keys_proven=proven,
        )
    return InclusionReport(
        ok=True,
        detail=(
            f"{proven} key(s) under root {root_s[:16]}… "
            f"confirmed={conf}"
        ),
        root_hash=root_s,
        confirmed=conf,
        keys_proven=proven,
    )


def proof_entries(proof_resp: dict[str, Any]) -> list[dict]:
    """Per-key proof objects from a ``get_proof`` response.

    Documented shape (CHIA_DATALAYER_RPC.md §4):
        {"proof": {"coin_id", "inner_puzzle_hash",
                   "store_proofs": {"store_id", "proofs": [ {...}, ... ]}}}
    Each entry has ``key_clvm_hash``, ``value_clvm_hash``, ``node_hash``,
    ``layers``. Tolerant of ``store_proofs`` appearing at the top level in case
    a client version flattens it.
    """
    proof = proof_resp.get("proof")
    store_proofs: Any = None
    if isinstance(proof, dict):
        store_proofs = proof.get("store_proofs")
    if store_proofs is None:
        store_proofs = proof_resp.get("store_proofs")
    if isinstance(store_proofs, dict):
        proofs = store_proofs.get("proofs")
        if isinstance(proofs, list):
            return [p for p in proofs if isinstance(p, dict)]
    return []


def _count_proven_keys(proof_resp: dict[str, Any], keys: list[str]) -> int:
    """How many of ``keys`` the proof actually covers.

    A key is proven iff its CLVM hash (``key_clvm_hash``, §4) appears among the
    proof entries. We never assume a non-empty blob proves an unmatched key —
    that blanket fallback previously reported inclusion nothing verified.
    """
    if not proof_resp.get("success", True):
        return 0

    proven_hashes = {
        str(e.get("key_clvm_hash", "")).lower()
        for e in proof_entries(proof_resp)
        if e.get("key_clvm_hash")
    }
    if not proven_hashes:
        return 0

    count = 0
    for k in keys:
        try:
            h = key_clvm_hash(k)
        except ValueError:
            continue  # not valid hex — cannot correspond to a stored key
        if h in proven_hashes:
            count += 1
    return count
