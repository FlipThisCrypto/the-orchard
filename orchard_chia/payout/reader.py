# SPDX-License-Identifier: Apache-2.0
"""Read signed attestations from the Chia DataLayer store.

Discovers every key in the store, filters to those that match the
``attest:<NODE>:<SEASON:08d>`` shape, then fetches and parses each.
Returns a list of ``(node_id, season, signed_payload)`` tuples.

Signature verification is the caller's job — see
``orchard_chia.datalayer.attest.verify_signature``.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..datalayer.attest import parse_datalayer_value
from ..datalayer.rpc import DataLayerRpc


KEY_PREFIX = "attest:"


@dataclass
class StoredAttestation:
    node_id: str
    season: int
    key_hex: str
    value_hex: str
    signed: dict


def _decode_key(key_hex: str) -> tuple[str, int] | None:
    """Decode the ASCII key form back to (node_id, season). Returns
    None if the key isn't an Orchard attestation key."""
    try:
        s = bytes.fromhex(key_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not s.startswith(KEY_PREFIX):
        return None
    rest = s[len(KEY_PREFIX):]
    try:
        node_id, season_str = rest.rsplit(":", 1)
        return node_id.upper(), int(season_str)
    except (ValueError, IndexError):
        return None


def read_all_attestations(
    rpc: DataLayerRpc,
    store_id: str,
) -> list[StoredAttestation]:
    """Pull every Orchard attestation currently in the store.

    Skips keys that don't match the prefix, and skips values that don't
    parse as JSON. Returns the surviving rows.
    """
    out: list[StoredAttestation] = []
    # STRICT read. The soft get_keys returns [] for an unreachable store,
    # and this function's caller turns an empty list into "payable: 0 /
    # refused: 0" and exits 0 — a payout run that reads NOTHING reporting
    # itself as a correct run that owed nobody. Measured live before the
    # pagination fix, that was every run. An unreadable store must be an
    # error, never an empty ledger.
    keys = rpc.get_keys_strict(store_id)
    for key_hex in keys:
        decoded = _decode_key(key_hex)
        if decoded is None:
            continue
        node_id, season = decoded
        value_hex = rpc.get_value(store_id, key_hex)
        if not value_hex:
            continue
        signed = parse_datalayer_value(value_hex)
        if not signed:
            continue
        out.append(StoredAttestation(
            node_id=node_id,
            season=season,
            key_hex=key_hex,
            value_hex=value_hex,
            signed=signed,
        ))
    return sorted(out, key=lambda s: (s.node_id, s.season))
