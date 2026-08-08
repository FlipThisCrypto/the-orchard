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

    A leading ``0x`` is accepted and stripped. DataLayer hex travels both ways in
    this codebase — ``batch_update`` keys are bare, everything the node returns is
    0x-prefixed — and ``bytes.fromhex`` rejects the prefix. Because
    ``_count_proven_keys`` catches ValueError and treats the key as unproven, a
    prefixed key did not raise: it silently reported "proof covers 0/N keys" on
    data that was fully proven on chain. That is the worst shape of bug here —
    a verifier that says CANNOT-VERIFY about something it could verify.
    Found on the first real publish (2026-08-08, node D8641AD6…, season 74 hour
    14), where the same proof verified by hand with current_root: true.
    """
    return hashlib.sha256(
        b"\x01" + bytes.fromhex(_strip0x(key_hex))
    ).hexdigest()


def _strip0x(h: str) -> str:
    s = str(h).strip()
    return s[2:] if s[:2].lower() == "0x" else s


# A DataLayer value is hashed by the same CLVM atom rule as a key.
clvm_hash = key_clvm_hash


@dataclass
class InclusionReport:
    ok: bool
    detail: str
    root_hash: str | None = None
    confirmed: bool | None = None
    keys_proven: int = 0
    current_root: bool | None = None
    values_bound: int = 0
    # True  => the result could not be established (RPC down, root not yet
    #          confirmed, key not published yet, proof stale) — retry later.
    # False on a failure => a definitive contradiction (an on-chain value that
    #          differs from the record being verified) — i.e. tampering.
    cannot_verify: bool = False


def proof_envelope(proof_resp: dict[str, Any]) -> dict | None:
    """The ``coin_id`` / ``inner_puzzle_hash`` / ``store_proofs`` a ``get_proof``
    response carries — exactly the arguments ``verify_proof`` needs. Returns
    ``None`` if any are missing (an unusable proof)."""
    proof = proof_resp.get("proof")
    if not isinstance(proof, dict):
        return None
    coin_id = proof.get("coin_id")
    iph = proof.get("inner_puzzle_hash")
    store_proofs = proof.get("store_proofs")
    if coin_id is None or iph is None or store_proofs is None:
        return None
    return {
        "coin_id": coin_id,
        "inner_puzzle_hash": iph,
        "store_proofs": store_proofs,
    }


def check_inclusion(
    rpc: DataLayerRpc,
    store_id: str,
    key_hex_list: list[str],
    *,
    expected_values: dict[str, str] | None = None,
) -> InclusionReport:
    """Return whether DataLayer reports a confirmed root + proof for keys.

    A confirmed on-chain root is required: ``get_root``'s ``confirmed`` must be
    exactly ``True``. A ``False`` or missing status fails closed (cannot assert
    permanence), and a later ``current_root == True`` does not override it.

    When ``expected_values`` (a ``key_hex -> value_hex`` map) is given, inclusion
    additionally binds each proven key to that value: the proof's
    ``value_clvm_hash`` must equal ``clvm_hash(expected_value)``. Binding is
    all-or-nothing — **every** requested key must have an expected value and
    every one must match (``values_bound == len(key_hex_list)``); a missing entry
    or a mismatch fails. This ties the proof to the data, not just the key. Omit
    ``expected_values`` entirely (``None``) to keep the key-only check (backward
    compatible); passing ``{}`` while requesting keys fails, by design.
    """
    if not key_hex_list:
        return InclusionReport(ok=False, detail="no keys to prove", cannot_verify=True)

    try:
        root_resp = rpc.get_root(store_id)
    except ChiaRpcError as e:
        return InclusionReport(
            ok=False, detail=f"get_root failed: {e}", cannot_verify=True
        )

    root_hash = (
        root_resp.get("hash")
        or root_resp.get("root_hash")
        or root_resp.get("root")
    )
    root_s = root_hash if isinstance(root_hash, str) else None

    # Enforce on-chain confirmation. get_root's ``confirmed`` must be exactly
    # True: a False status means the root's transaction is still pending, and a
    # MISSING status is unknown — both fail closed (cannot assert permanence)
    # rather than being treated as confirmed. verify_proof's current_root
    # (checked later) does NOT substitute for a confirmed root.
    confirmed_raw = root_resp.get("confirmed")
    conf = confirmed_raw is True

    if not root_s:
        return InclusionReport(
            ok=False,
            detail="get_root returned no hash",
            root_hash=None,
            confirmed=conf,
            cannot_verify=True,
        )
    if not conf:
        return InclusionReport(
            ok=False,
            detail=(
                f"store root not confirmed on-chain (confirmed={confirmed_raw!r}); "
                f"cannot assert permanence"
            ),
            root_hash=root_s,
            confirmed=False,
            cannot_verify=True,
        )

    try:
        proof_resp = rpc.get_proof(store_id, key_hex_list)
    except ChiaRpcError as e:
        return InclusionReport(
            ok=False,
            detail=f"get_proof failed: {e}",
            root_hash=root_s,
            confirmed=conf,
            cannot_verify=True,
        )

    proven = _count_proven_keys(proof_resp, key_hex_list)

    if proven < len(key_hex_list):
        # Key not (yet) provable on chain — unpublished/pending, not tampering.
        return InclusionReport(
            ok=False,
            detail=(
                f"proof covers {proven}/{len(key_hex_list)} key(s); "
                f"root={root_s[:16]}… confirmed={conf}"
            ),
            root_hash=root_s,
            confirmed=conf,
            keys_proven=proven,
            cannot_verify=True,
        )

    # Keys are covered — now prove the proof chains to the CURRENT on-chain root.
    # get_root returning a hash is not enough; only verify_proof's current_root
    # attests the data is under the published root right now (SPEC §7 check 1).
    env = proof_envelope(proof_resp)
    if env is None:
        return InclusionReport(
            ok=False,
            detail="proof missing coin_id/inner_puzzle_hash/store_proofs; "
                   "cannot call verify_proof",
            root_hash=root_s,
            confirmed=conf,
            keys_proven=proven,
            cannot_verify=True,
        )
    try:
        vp = rpc.verify_proof(
            env["coin_id"], env["inner_puzzle_hash"], env["store_proofs"]
        )
    except ChiaRpcError as e:
        return InclusionReport(
            ok=False,
            detail=f"verify_proof failed: {e}",
            root_hash=root_s,
            confirmed=conf,
            keys_proven=proven,
            cannot_verify=True,
        )

    current_root = bool(vp.get("current_root"))
    if not current_root:
        # Proof was valid when generated but the root has since moved — we
        # cannot assert present inclusion. Honest 'cannot-verify-now'.
        return InclusionReport(
            ok=False,
            detail=(
                f"{proven} key(s) proven but root moved since proof "
                f"(current_root=false); re-fetch to confirm"
            ),
            root_hash=root_s,
            confirmed=conf,
            keys_proven=proven,
            current_root=False,
            cannot_verify=True,
        )

    # Bind the proof to the actual data: the on-chain value_clvm_hash for each
    # proven key must match the value of the record we are verifying. Without
    # this, a valid key proof says nothing about whether the stored value is the
    # one being displayed.
    values_bound = 0
    if expected_values is not None:
        # Normalize BOTH sides of every comparison. The node returns 0x-prefixed
        # hashes; our clvm_hash() returns bare. Comparing the two forms directly
        # made `.get()` miss, `on_chain` come back None, and `None != want` be
        # reported as "on-chain value differs (tampered or stale mirror)" —
        # i.e. a verdict of INVALID against data that was byte-for-byte correct
        # on chain. A verifier crying tampering at good data is worse than one
        # that cannot verify: it destroys trust in the thing it is meant to
        # establish. Caught on the first real publish (2026-08-08).
        val_by_keyhash = {
            _strip0x(kh): _strip0x(vh)
            for kh, vh in proof_value_hashes(proof_resp).items()
        }
        missing_expected: list[str] = []
        mismatched: list[str] = []
        for k in key_hex_list:
            exp = expected_values.get(k)
            if exp is None:
                # A requested key with no expected value supplied — we cannot
                # bind it, so we must not assert inclusion for it.
                missing_expected.append(k)
                continue
            try:
                on_chain = val_by_keyhash.get(_strip0x(key_clvm_hash(k)))
                want = _strip0x(clvm_hash(exp))
            except ValueError:
                mismatched.append(k)
                continue
            if on_chain is not None and on_chain == want:
                values_bound += 1
            else:
                mismatched.append(k)
        if missing_expected:
            # Our side couldn't supply a value for a requested key — a coverage
            # gap on the verifier, not proof of tampering.
            return InclusionReport(
                ok=False,
                detail=(
                    f"no expected value supplied for {len(missing_expected)} of "
                    f"{len(key_hex_list)} requested key(s); refusing to assert "
                    f"inclusion without full value coverage"
                ),
                root_hash=root_s,
                confirmed=conf,
                keys_proven=proven,
                current_root=True,
                values_bound=values_bound,
                cannot_verify=True,
            )
        if mismatched:
            return InclusionReport(
                ok=False,
                detail=(
                    f"{len(mismatched)} key(s) proven but on-chain value differs "
                    f"from the record being verified (tampered or stale mirror)"
                ),
                root_hash=root_s,
                confirmed=conf,
                keys_proven=proven,
                current_root=True,
                values_bound=values_bound,
            )
        if values_bound != len(key_hex_list):
            # Defensive: every requested key must be value-bound to succeed.
            return InclusionReport(
                ok=False,
                detail=(
                    f"bound {values_bound}/{len(key_hex_list)} value(s); "
                    f"incomplete value coverage"
                ),
                root_hash=root_s,
                confirmed=conf,
                keys_proven=proven,
                current_root=True,
                values_bound=values_bound,
                cannot_verify=True,
            )

    bound_note = (
        f", {values_bound} value(s) bound" if expected_values is not None else ""
    )
    return InclusionReport(
        ok=True,
        detail=(
            f"{proven} key(s) verified against current root {root_s[:16]}… "
            f"confirmed={conf}{bound_note}"
        ),
        root_hash=root_s,
        confirmed=conf,
        keys_proven=proven,
        current_root=True,
        values_bound=values_bound,
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


def proof_value_hashes(proof_resp: dict[str, Any]) -> dict[str, str]:
    """Map ``key_clvm_hash -> value_clvm_hash`` from a ``get_proof`` response,
    lowercased. Used to bind a proven key to its stored value's hash."""
    out: dict[str, str] = {}
    for e in proof_entries(proof_resp):
        k = str(e.get("key_clvm_hash", "")).lower()
        v = str(e.get("value_clvm_hash", "")).lower()
        if k:
            out[k] = v
    return out


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
        # The node returns 0x-prefixed hashes; ours are bare. Compare on a
        # normalized form, or every key looks unproven against a proof that
        # actually covers it.
        if _strip0x(h) in {_strip0x(p) for p in proven_hashes}:
            count += 1
    return count
