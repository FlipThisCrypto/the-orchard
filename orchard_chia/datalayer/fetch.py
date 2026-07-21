# SPDX-License-Identifier: Apache-2.0
"""Fetch an Orchard verification bundle from a live DataLayer store.

Assembles the same shape ``verify.verify_bundle`` expects:

    meta, node, attest, readings_records

Pure I/O over ``DataLayerRpc`` — no crypto here.
"""
from __future__ import annotations

from . import schema
from .rpc import DataLayerRpc


class FetchError(RuntimeError):
    """Raised when a required key is missing or unreadable."""


def fetch_bundle(
    rpc: DataLayerRpc,
    store_id: str,
    *,
    node_id: str,
    season: int,
    hours: list[int] | None = None,
) -> dict:
    """Load meta + node + attest + the requested readings hours.

    If ``hours`` is None, discovers every ``readings:<node>:<season>:*`` key
    present in the store (via ``get_keys``).
    """
    node_id = node_id.upper()
    season = int(season)
    if not (store_id or '').strip():
        raise FetchError('store_id is empty')

    def _get(key_hex: str) -> dict | None:
        return schema.parse_value(rpc.get_value(store_id, key_hex))

    meta = _get(schema.meta_key())
    if meta is None:
        raise FetchError("meta:schema missing from store")

    node = _get(schema.node_key(node_id))
    if node is None:
        raise FetchError(f"node:{node_id} missing from store")

    attest = _get(schema.attest_key(node_id, season))
    if attest is None:
        raise FetchError(f"attest:{node_id}:{season:08d} missing from store")

    if hours is None:
        hours = _discover_hours(rpc, store_id, node_id, season)
    if not hours:
        raise FetchError(
            f"no readings:{node_id}:{season:08d}:* keys found — "
            "pass --hour or publish hot-path batches first"
        )

    readings_records: list[dict] = []
    for h in sorted(int(x) for x in hours):
        rec = _get(schema.readings_key(node_id, season, h))
        if rec is None:
            raise FetchError(
                f"readings:{node_id}:{season:08d}:{h:02d} missing from store"
            )
        readings_records.append(rec)

    return {
        "meta": meta,
        "node": node,
        "attest": attest,
        "readings_records": readings_records,
    }


def _discover_hours(
    rpc: DataLayerRpc,
    store_id: str,
    node_id: str,
    season: int,
) -> list[int]:
    prefix = f"readings:{node_id.upper()}:{int(season):08d}:"
    found: list[int] = []
    for key_hex in rpc.get_keys(store_id):
        try:
            ascii_key = bytes.fromhex(key_hex).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if not ascii_key.startswith(prefix):
            continue
        tail = ascii_key[len(prefix):]
        if len(tail) == 2 and tail.isdigit():
            found.append(int(tail))
    return sorted(found)
