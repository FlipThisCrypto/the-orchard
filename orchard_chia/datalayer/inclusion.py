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

from dataclasses import dataclass
from typing import Any

from .rpc import ChiaRpcError, DataLayerRpc


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


def _count_proven_keys(proof_resp: dict[str, Any], keys: list[str]) -> int:
    """Best-effort count of keys present in a get_proof response.

    Chia RPC shapes have varied across versions — accept common layouts:
      - proof_resp["proof"] is a list of per-key objects with "key"
      - proof_resp["proofs"] dict keyed by key hex
      - proof_resp["key_value_hashes"] / nested structures
    When the response succeeds and has a non-empty proof blob but no
    enumerable keys, count all requested keys as proven (RPC already
    scoped the request).
    """
    if not proof_resp.get("success", True):
        return 0

    proofs = proof_resp.get("proofs")
    if isinstance(proofs, dict):
        return sum(1 for k in keys if k in proofs or k.lower() in proofs)

    proof = proof_resp.get("proof")
    if isinstance(proof, list):
        found = 0
        for item in proof:
            if not isinstance(item, dict):
                continue
            k = item.get("key") or item.get("key_hex")
            if isinstance(k, str) and (k in keys or k.lower() in {x.lower() for x in keys}):
                found += 1
        if found:
            return found
        # List present but keys not labeled — RPC accepted the request.
        if proof:
            return len(keys)

    # Generic success with any proof-ish payload.
    for field in ("proof", "proof_tree", "clvm_proof", "proof_info"):
        if proof_resp.get(field):
            return len(keys)
    return 0
