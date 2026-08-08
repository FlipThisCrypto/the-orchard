# SPDX-License-Identifier: Apache-2.0
"""Orchard Pass ownership verification.

Used by the oracle's ``/register`` endpoint (Phase 6.5, deferred) and
the payout script (Phase 7) to gate operator actions on holding a
Pass.

Two verification paths:

  1) **Local wallet** (``wallet_holds_pass`` / ``list_owned_passes``) —
     queries the operator's own Chia wallet daemon via mTLS RPC. Fast
     and trustless when the Pass is in a key the daemon controls.

  2) **Chain indexer** (``list_passes_by_address``) — queries the
     MintGarden public API by XCH address. Works for ANY address
     regardless of which local key holds it, so the credential wallet
     can be a totally separate key from the daemon's active fingerprint.
     Trust assumption: MintGarden is honestly indexing the chain.

The local-wallet path is preferred when available because it has no
network dependency on a third party. The indexer path is the right
default for novice operators who keep credentials and issuance in
separate wallets (e.g. cold-storage Pass holder + hot-storage attester).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from .generate import (
    ORCHARD_GENESIS_COLLECTION_ID,
    ORCHARD_GENESIS_COLLECTION_BECH32_ID,
)
from ..wallet.rpc import WalletRpc


# MintGarden public REST API. Read-only, no auth.
MINTGARDEN_API_BASE = "https://api.mintgarden.io"

# Identify ourselves. urllib sends "Python-urllib/3.x" by default, which
# MintGarden's edge blocks outright — every Pass verification failed with
# "indexer error: MintGarden … -> HTTP 403". Reproduced from an unrelated IP:
# same URL, same library, default UA -> 403, a real UA -> 200. So it is the
# header, not the address, and not rate limiting.
#
# This gates registration, so a blanket 403 means nobody can bind a wallet to a
# Tree. Worth stating plainly: it is polite to identify a bot, and it is the
# difference between working and not.
USER_AGENT = "TheOrchard-Oracle/1.0 (+https://theorchard.network)"
# Per-request page size — collection has 10 items so one page covers it;
# but we page anyway in case the collection grows.
INDEXER_PAGE_SIZE = 50


class IndexerError(RuntimeError):
    """Raised when the indexer call itself fails (HTTP, timeout, JSON
    decode). Distinct from `WalletRpcError` so callers can decide
    policy per source."""


def _iter_owned_nfts(rpc: WalletRpc, wallet_id: int,
                     batch: int = 50) -> Iterable[dict]:
    """Page through every NFT this wallet owns."""
    start = 0
    while True:
        items = rpc.nft_get_nfts(wallet_id=wallet_id, start_index=start, num=batch)
        if not items:
            return
        for it in items:
            yield it
        if len(items) < batch:
            return
        start += len(items)


def _nft_collection_id(nft_info: dict) -> str | None:
    """CHIP-7 collection.id field, dug out of whatever shape the wallet
    RPC returned. Tolerant of small shape differences across releases.
    """
    # Modern Chia wallet returns metadata_url-resolved fields directly.
    coll = nft_info.get("collection") or {}
    if isinstance(coll, dict):
        cid = coll.get("id")
        if cid:
            return str(cid)
    # Some versions surface the parsed metadata under metadata_json.
    md = nft_info.get("metadata_json") or {}
    if isinstance(md, dict):
        coll = md.get("collection") or {}
        if isinstance(coll, dict):
            cid = coll.get("id")
            if cid:
                return str(cid)
    return None


def _matches_genesis(cid: str | None) -> bool:
    """Accept either the CHIP-7 UUID or the bech32 marketplace id —
    different RPCs / indexers surface different forms of the same
    on-chain collection, and we want both to satisfy ownership."""
    if not cid:
        return False
    return cid in (ORCHARD_GENESIS_COLLECTION_ID,
                   ORCHARD_GENESIS_COLLECTION_BECH32_ID)


def wallet_holds_pass(rpc: WalletRpc, *, nft_wallet_id: int,
                     collection_id: str = ORCHARD_GENESIS_COLLECTION_ID,
                     ) -> bool:
    """Return True if any NFT in ``nft_wallet_id`` belongs to
    ``collection_id``.

    Raises WalletRpcError if the wallet daemon is unreachable; caller
    should treat that as "could not verify" and decide policy (deny vs
    allow-when-uncheckable) at the call site.

    SECURITY (M7): this local-wallet path matches on the NFT metadata's
    self-declared ``collection.id``, which an attacker can set on an NFT
    they mint themselves — so it is a convenience self-check, NOT a
    spoof-resistant gate. The authoritative ownership gate (oracle
    /register + reward eligibility) is the indexer path
    (``list_passes_by_address``), where collection membership is resolved
    from the on-chain creator DID by MintGarden. Binding this path to
    ``ORCHARD_GENESIS_CREATOR_DID`` on chain is a follow-up.
    """
    for nft in _iter_owned_nfts(rpc, nft_wallet_id):
        nft_cid = _nft_collection_id(nft)
        if collection_id == ORCHARD_GENESIS_COLLECTION_ID:
            if _matches_genesis(nft_cid):
                return True
        elif nft_cid == collection_id:
            return True
    return False


def list_owned_passes(rpc: WalletRpc, *, nft_wallet_id: int,
                     collection_id: str = ORCHARD_GENESIS_COLLECTION_ID,
                     ) -> list[dict]:
    """Like wallet_holds_pass but returns all matching NFT records."""
    out: list[dict] = []
    for nft in _iter_owned_nfts(rpc, nft_wallet_id):
        nft_cid = _nft_collection_id(nft)
        if collection_id == ORCHARD_GENESIS_COLLECTION_ID:
            if _matches_genesis(nft_cid):
                out.append(nft)
        elif nft_cid == collection_id:
            out.append(nft)
    return out


# ----------------------------------------------------------------------
# Indexer (MintGarden) path
# ----------------------------------------------------------------------

# Cap on pages we'll walk before declaring the collection implausibly large —
# a runaway guard, far above any real Orchard Pass collection.
INDEXER_MAX_PAGES = 100


def _fetch_mintgarden_collection_items(
    collection_bech32_id: str = ORCHARD_GENESIS_COLLECTION_BECH32_ID,
    *,
    api_base: str = MINTGARDEN_API_BASE,
    page_size: int = INDEXER_PAGE_SIZE,
    timeout: int = 30,
    _opener=None,
) -> list[dict]:
    """Fetch EVERY NFT in a MintGarden collection, paging through all of it.

    MintGarden returns a `next` cursor (opaque token) on each page; we pass it
    back as ``?page=<cursor>`` until it's exhausted. The cursor field name is
    read defensively (``next`` / ``next_cursor`` / ``cursor``).

    Safety — this must NEVER silently miss a Pass (it gates registration):
      * If a page is the last one (fewer than ``page_size`` items) we stop —
        the collection is fully read.
      * If we get a FULL page but find no cursor we recognize, we cannot prove
        the collection is complete, so we raise ``IndexerError`` rather than
        truncate. (At today's tiny collection a single short page ends cleanly.)
      * A runaway is capped at ``INDEXER_MAX_PAGES`` with a raise.

    ``_opener`` is a hook for tests — a callable ``(url)->bytes``.
    """
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(INDEXER_MAX_PAGES):
        url = f"{api_base}/collections/{collection_bech32_id}/nfts?size={page_size}"
        if cursor:
            url += f"&page={urllib.parse.quote(str(cursor), safe='')}"
        try:
            raw = (_opener(url) if _opener
                   else urllib.request.urlopen(
                       urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                       timeout=timeout,
                   ).read())
        except urllib.error.HTTPError as e:
            hint = (
                "  (403 usually means the request was not identified — check the "
                "User-Agent)" if e.code == 403 else ""
            )
            raise IndexerError(f"MintGarden {url} -> HTTP {e.code}{hint}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise IndexerError(f"MintGarden {url} unreachable: {e}") from e
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise IndexerError(f"MintGarden {url} non-JSON: {e}") from e

        page = list(doc.get("items") or [])
        items.extend(page)
        cursor = doc.get("next") or doc.get("next_cursor") or doc.get("cursor")

        if len(page) < page_size:
            return items          # short page => last page, fully read
        if not cursor:
            # Full page but no cursor we understand — can't confirm we got it
            # all. Fail loud rather than silently drop Passes past this page.
            raise IndexerError(
                f"MintGarden returned a full page ({page_size}) for "
                f"{collection_bech32_id} with no recognized pagination cursor; "
                "refusing to risk a truncated Pass list — wire the cursor field."
            )
    raise IndexerError(
        f"MintGarden collection {collection_bech32_id} exceeded "
        f"{INDEXER_MAX_PAGES} pages; aborting as implausibly large.")


def _normalize_indexer_item(item: dict) -> dict:
    """Make a MintGarden NFT dict look enough like the wallet-RPC one
    that the caller's display code doesn't have to branch.
    """
    return {
        # Wallet RPC uses 'nft_coin_id'/'launcher_id' (hex). MintGarden
        # uses 'encoded_id' (nft1... bech32) and 'id' (hex). Surface
        # both so callers can take whichever they prefer.
        "nft_coin_id":     item.get("encoded_id"),
        "launcher_id":     item.get("id"),
        "name":            item.get("name"),
        "edition_number":  item.get("edition_number"),
        "edition_total":   item.get("edition_total"),
        "owner_address":   item.get("owner_address_encoded_id"),
        "collection_id":   item.get("collection_id"),
        "_source":         "mintgarden",
    }


def list_passes_by_address(
    address: str,
    *,
    collection_bech32_id: str = ORCHARD_GENESIS_COLLECTION_BECH32_ID,
    _opener=None,
) -> list[dict]:
    """Return all Orchard Passes currently owned by ``address``.

    Uses the MintGarden public API. Works for any address whether or
    not the local wallet daemon holds its key. Caller should treat an
    ``IndexerError`` as "could not verify" — same policy stance as the
    wallet RPC path.
    """
    address = address.strip()
    items = _fetch_mintgarden_collection_items(collection_bech32_id,
                                               _opener=_opener)
    out: list[dict] = []
    for it in items:
        owner = it.get("owner_address_encoded_id")
        if not (owner and owner == address):
            continue
        # Defensive (M7): the collection endpoint should only return
        # genuine members, but re-check the collection id so a future API
        # change or a confused response can't slip a non-genesis NFT past
        # the gate this backs (oracle /register + reward eligibility).
        item_cid = it.get("collection_id")
        if item_cid and item_cid != collection_bech32_id:
            continue
        out.append(_normalize_indexer_item(it))
    # Sort by edition_number for stable display.
    out.sort(key=lambda r: (r.get("edition_number") or 0,
                            r.get("name") or ""))
    return out


def address_holds_pass(
    address: str,
    *,
    collection_bech32_id: str = ORCHARD_GENESIS_COLLECTION_BECH32_ID,
    _opener=None,
) -> bool:
    """True if ``address`` owns at least one Pass in the collection."""
    return bool(list_passes_by_address(
        address,
        collection_bech32_id=collection_bech32_id,
        _opener=_opener,
    ))
