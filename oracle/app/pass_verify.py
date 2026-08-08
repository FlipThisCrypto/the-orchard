# SPDX-License-Identifier: Apache-2.0
"""Orchard Pass ownership lookup for /register.

Thin wrapper around ``orchard_chia.nft.verify.list_passes_by_address``
that adds a small in-memory cache so a wallet checked once doesn't hit
MintGarden again for ``CACHE_TTL_SECONDS``. Two reasons that matters:

1. The dashboard wizard calls verify twice per provisioning attempt
   (once on the operator's "Verify" button click, once when /register
   re-checks server-side). Without a cache, that's two ~700ms indexer
   round-trips back to back.

2. A flaky operator retrying registration 10 times in 30 seconds
   shouldn't drive 10 MintGarden requests per attempt.

The cache is per-process and module-scoped; restart wipes it. For a
multi-worker deployment we'd back this with Redis, but v1 oracle is
single-uvicorn-worker so this is fine.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def _nft_verify():
    """Import the sibling orchard_chia NFT verifier lazily.

    It was imported at module scope, and register.py imports this module at app
    startup — so the oracle transitively required orchard_chia AND its `requests`
    / `urllib3` dependencies just to BOOT, even though they are only needed when
    a Pass is actually verified. Those two packages are not declared in
    oracle/requirements.{txt,lock}, so a clean oracle-only install crashed on
    startup with an ImportError. Deferring the import means a missing optional
    dependency degrades to an actionable 503 on /register instead of taking the
    whole service down (readings ingestion keeps working).
    """
    from orchard_chia.nft import verify as nft_verify  # noqa: PLC0415
    return nft_verify

# 5 minutes is a comfortable balance: long enough to amortize a
# registration retry storm or a wizard verify/register pair, short
# enough that a Pass-just-acquired wallet sees the new state within
# coffee-break time.
CACHE_TTL_SECONDS = 300


class PassVerifyError(RuntimeError):
    """Raised when verification could not be performed (indexer down,
    network error, etc.). Distinct from "verified clean, no Pass" —
    callers usually decide policy per case."""


_cache: dict[str, tuple[list[dict], float]] = {}
_cache_lock = threading.Lock()


def _get_cached(address: str) -> list[dict] | None:
    with _cache_lock:
        entry = _cache.get(address)
        if entry is None:
            return None
        passes, t = entry
        if time.monotonic() - t > CACHE_TTL_SECONDS:
            _cache.pop(address, None)
            return None
        # Return a shallow copy so callers can't mutate the cache.
        return list(passes)


def _put_cache(address: str, passes: list[dict]) -> None:
    with _cache_lock:
        _cache[address] = (list(passes), time.monotonic())


def clear_cache() -> None:
    """Test helper — drop the whole cache."""
    with _cache_lock:
        _cache.clear()


def list_passes_for_address(address: str) -> list[dict]:
    """Return the Orchard Passes currently held by ``address``.

    Cached for ``CACHE_TTL_SECONDS`` per address. Raises ``PassVerifyError``
    if the indexer call itself failed (network, HTTP non-2xx, JSON
    parse failure). An empty list means "indexer answered, this wallet
    holds zero Passes" — that's a clean "no Pass" answer, not an error.
    """
    address = address.strip()
    cached = _get_cached(address)
    if cached is not None:
        return cached
    try:
        nft_verify = _nft_verify()
    except ImportError as e:  # optional dep missing — actionable, not a crash
        raise PassVerifyError(
            f"Orchard Pass verification is unavailable: {e}. Install the oracle's "
            f"requirements (requests, urllib3) or omit wallet binding."
        ) from e
    try:
        passes = nft_verify.list_passes_by_address(address)
    except nft_verify.IndexerError as e:
        raise PassVerifyError(str(e)) from e
    _put_cache(address, passes)
    return passes


def first_pass_nft_id(address: str) -> str | None:
    """Convenience: return the bech32 nft1... id of the first Pass the
    wallet holds, or None if it holds zero. Suitable for binding to a
    single Tree.

    "First" is determined by the indexer's natural order (which we
    sort by edition_number in the verify module). When an operator
    holds multiple Passes, the lowest edition_number gets bound; this
    is arbitrary but deterministic.
    """
    passes = list_passes_for_address(address)
    if not passes:
        return None
    # The indexer normalizer surfaces nft_coin_id as the bech32 nft1...
    # for consumers that prefer the pretty id; launcher_id is the hex.
    p = passes[0]
    return p.get("nft_coin_id") or p.get("launcher_id")


def utcnow() -> datetime:
    """Mockable wall clock for tests."""
    return datetime.now(timezone.utc)
