# SPDX-License-Identifier: Apache-2.0
"""TLS-wrapped HTTP clients for Chia full-node and DataLayer RPCs.

Chia's RPC endpoints use HTTPS with mutual TLS. Operator's client
cert + key path go into ``chia/config.yaml``; we present them on every
request. ``verify=False`` because Chia's CA is self-signed and our
local connections are on localhost only.

If you push this to a multi-host setup later, switch to passing the
operator's CA cert path via ``verify=<ca_path>`` instead.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import requests
import urllib3

from .retry import RetryPolicy, call_with_retry

# Local-only mTLS to a self-signed CA — silence the legitimate-but-noisy
# warning about disabled host verification.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ChiaRpcError(RuntimeError):
    """Raised for any non-2xx Chia RPC response or transport failure."""


# verify=False is only safe on localhost; refuse it over a network where
# a MITM could impersonate the full node / DataLayer service.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class _Endpoint:
    host: str
    port: int
    cert_path: str
    key_path: str

    def url(self, route: str) -> str:
        return f"https://{self.host}:{self.port}/{route.lstrip('/')}"


class FullNodeRpc:
    """Subset of Chia full-node RPC the attestation writer needs."""

    def __init__(self, host: str, port: int, cert_path: str, key_path: str):
        self._ep = _Endpoint(host, port, cert_path, key_path)

    def _post(self, route: str, body: dict) -> dict:
        if self._ep.host not in _LOOPBACK_HOSTS:
            raise ChiaRpcError(
                f"refusing verify=False against non-loopback host "
                f"{self._ep.host!r}: Chia RPC over a network needs real TLS "
                f"verification. Only localhost mTLS may disable it."
            )
        try:
            r = requests.post(
                self._ep.url(route),
                json=body,
                cert=(self._ep.cert_path, self._ep.key_path),
                verify=False,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ChiaRpcError(f"full_node {route} unreachable: {e}") from e
        if r.status_code != 200:
            raise ChiaRpcError(f"full_node {route} -> {r.status_code}: {r.text}")
        data = r.json()
        if not data.get("success", True):
            raise ChiaRpcError(f"full_node {route} returned success=false: {data}")
        return data

    def get_blockchain_state(self) -> dict:
        return self._post("get_blockchain_state", {})

    def peak_height(self) -> int:
        st = self.get_blockchain_state()
        # blockchain_state.peak.height is the current synced height.
        peak = st.get("blockchain_state", {}).get("peak") or {}
        return int(peak.get("height", 0))

    def get_block_record_by_height(self, height: int) -> dict | None:
        """Block record at a height. ``timestamp`` is only present on
        transaction blocks (null otherwise). See CHIA_DATALAYER_RPC.md §Full-node."""
        data = self._post("get_block_record_by_height", {"height": int(height)})
        return data.get("block_record")

    def get_block_record(self, header_hash: str) -> dict | None:
        """Block record by header hash (the block identifier)."""
        data = self._post("get_block_record", {"header_hash": header_hash})
        return data.get("block_record")

    def get_block_records(self, start: int, end: int) -> list[dict]:
        """Block records for heights ``[start, end)`` (end non-inclusive)."""
        data = self._post(
            "get_block_records", {"start": int(start), "end": int(end)}
        )
        return data.get("block_records", []) or []


class DataLayerRpc:
    """Subset of Chia DataLayer RPC the attestation writer needs.

    Transient transport / 5xx failures are retried with exponential
    backoff (Round 3 resilience). Last attempt counts and retry totals
    are exposed on ``last_retry_attempts`` / ``last_retried`` for ops logs.
    """

    def __init__(
        self,
        host: str,
        port: int,
        cert_path: str,
        key_path: str,
        *,
        retry_policy: RetryPolicy | None = None,
    ):
        self._ep = _Endpoint(host, port, cert_path, key_path)
        self._retry_policy = retry_policy or RetryPolicy.from_env()
        self.last_retry_attempts: int = 1
        self.last_retried: bool = False

    def _post_once(self, route: str, body: dict) -> dict:
        if self._ep.host not in _LOOPBACK_HOSTS:
            raise ChiaRpcError(
                f"refusing verify=False against non-loopback host "
                f"{self._ep.host!r}: Chia RPC over a network needs real TLS "
                f"verification. Only localhost mTLS may disable it."
            )
        try:
            r = requests.post(
                self._ep.url(route),
                json=body,
                cert=(self._ep.cert_path, self._ep.key_path),
                verify=False,
                timeout=120,  # batch_update can take a while
            )
        except requests.RequestException as e:
            raise ChiaRpcError(f"datalayer {route} unreachable: {e}") from e
        if r.status_code != 200:
            raise ChiaRpcError(f"datalayer {route} -> {r.status_code}: {r.text}")
        data = r.json()
        if not data.get("success", True):
            raise ChiaRpcError(f"datalayer {route} returned success=false: {data}")
        return data

    def _post(self, route: str, body: dict) -> dict:
        def _on_retry(attempt: int, exc: BaseException, wait: float) -> None:
            print(
                f"[orchard.datalayer] retry {route} after attempt {attempt}: "
                f"{type(exc).__name__}: {exc} — sleep {wait:.2f}s",
                file=sys.stderr,
            )

        result = call_with_retry(
            lambda: self._post_once(route, body),
            policy=self._retry_policy,
            on_retry=_on_retry,
        )
        self.last_retry_attempts = result.attempts
        self.last_retried = result.retried
        return result.value

    def batch_update(
        self,
        store_id: str,
        changelist: list[dict],
        *,
        fee: int | None = None,
        submit_on_chain: bool = True,
    ) -> dict:
        """Apply a list of insert/delete operations to a DataLayer store.

        ``changelist`` items look like:
            {"action": "insert", "key": "<hex>", "value": "<hex>"}
            {"action": "delete", "key": "<hex>"}

        ``fee`` is the transaction fee in **mojos** (1 XCH = 1e12 mojos). A
        higher fee helps the update's transaction get into a block when the
        mempool is congested — a 0-fee tx can stall indefinitely, after which
        our post-write confirm never sees the inserts and the watermark can't
        advance. Omitted (``None``) preserves the node's default (0). ``fee`` is
        only added to the request when set. See CHIA_DATALAYER_RPC.md §3.

        ``submit_on_chain=False`` stages the change locally without a
        transaction (default ``True`` submits on-chain).
        """
        body: dict[str, Any] = {"id": store_id, "changelist": changelist}
        if fee is not None:
            body["fee"] = int(fee)
        if not submit_on_chain:
            body["submit_on_chain"] = False
        return self._post("batch_update", body)

    def get_value(self, store_id: str, key_hex: str) -> str | None:
        body = {"id": store_id, "key": key_hex}
        try:
            # Reads use a shorter timeout path: still go through _post for
            # retry, but get_value failures are soft (None) for scanners.
            data = self._post("get_value", body)
        except ChiaRpcError:
            return None
        return data.get("value")

    def get_value_strict(self, store_id: str, key_hex: str) -> str:
        """Like get_value but raises ChiaRpcError instead of returning None."""
        data = self._post("get_value", {"id": store_id, "key": key_hex})
        val = data.get("value")
        if val is None:
            raise ChiaRpcError(f"datalayer get_value missing value for key {key_hex[:16]}…")
        return val

    def get_keys(self, store_id: str) -> list[str]:
        """All keys in the store, hex-encoded, across every page.

        DataLayer caps a single ``get_keys`` response at ``max_page_size``
        (default 40 MB). For a large store an un-paginated read silently
        truncates — which would make the payout reader miss ``attest:`` keys
        (operators underpaid) and the verifier miss reading hours. So page
        explicitly: fetch page 1, then follow ``total_pages`` to the end,
        concatenating (CHIA_DATALAYER_RPC.md §2).

        Backward-safe: a node that returns no ``total_pages`` held everything in
        one response, so we stop after page 1; a node that rejects the ``page``
        param falls back to an un-paginated read. Soft-fails to ``[]`` only when
        the store is unreachable on the first read (used by scanners). A failure
        *partway through* pagination raises rather than returning a truncated set.
        """
        try:
            first, first_page = self._first_key_page(store_id)
        except ChiaRpcError:
            # Node may reject the page param (or be unreachable) — try plain.
            try:
                data = self._post("get_keys", {"id": store_id})
            except ChiaRpcError:
                return []
            return list(data.get("keys", []))
        return self._drain_key_pages(store_id, first, first_page)

    def get_keys_strict(self, store_id: str) -> list[str]:
        """Like :meth:`get_keys` but PROPAGATES a first-page failure.

        ``get_keys`` soft-fails to ``[]`` for scanners, which makes "the store
        is unreachable" indistinguishable from "the store is empty". Any caller
        that would treat an empty list as evidence (e.g. sealing an attestation
        as "nothing was published") must use this instead.
        """
        first, first_page = self._first_key_page(store_id)
        return self._drain_key_pages(store_id, first, first_page)

    # -- pagination internals ------------------------------------------------
    #
    # `page` is 0-INDEXED. Asking for page 1 on a single-page store returns an
    # empty list rather than an error, which is the worst possible failure
    # shape: measured against the live store, `get_keys` returned 0 keys while
    # 200 were present. Every reader believed the dataset was empty — the
    # payout reader found no attestations and reported a clean run having paid
    # nobody, and the verifier found nothing to check.
    #
    # Rather than swapping one hardcoded guess for another, probe: ask for page
    # 0, and only fall back to a 1-indexed read if page 0 is rejected or looks
    # empty while the node says there is more. A node that changes convention
    # then costs a wasted request, not a silent dataset.

    def _first_key_page(self, store_id: str) -> tuple[dict, int]:
        """Fetch the first page under whichever indexing the node uses.

        A 1-indexed node can REJECT page 0 outright rather than answering it
        empty, so a failure here is information, not a dead end — it must lead
        to the other convention, never to an unpaginated read that quietly
        truncates a large store.
        """
        zero: dict | None = None
        try:
            zero = self._post("get_keys", {"id": store_id, "page": 0})
            if zero.get("keys"):
                return zero, 0
        except ChiaRpcError:
            zero = None                     # node rejects page 0 → 1-indexed

        # Page 0 was empty or refused. Either the store really is empty, or
        # this node counts from 1. Those are indistinguishable from one
        # response, so ask the other question rather than guessing.
        try:
            one = self._post("get_keys", {"id": store_id, "page": 1})
        except ChiaRpcError:
            if zero is None:
                raise                       # neither convention answered
            return zero, 0                  # 0-indexed, genuinely empty
        if one.get("keys") or zero is None:
            return one, 1
        return zero, 0

    def _drain_key_pages(self, store_id: str, first: dict, first_page: int) -> list[str]:
        keys = list(first.get("keys", []))
        try:
            total = int(first.get("total_pages"))
        except (TypeError, ValueError):
            return keys  # no pagination info — one response held everything

        if not keys and total > 1:
            # A store spanning several pages cannot have an empty first page,
            # so this response contradicts itself and the read is wrong.
            #
            # The bound is `> 1`, not `> 0`: an empty store legitimately reports
            # one page holding nothing, and from a single response that is
            # genuinely indistinguishable from a misread page. Distinguishing
            # them is the probe's job, not this guard's — this only catches the
            # case no probe could explain away. Returning [] here is how
            # "unreachable" and "empty" became the same answer.
            raise ChiaRpcError(
                f"get_keys returned no keys for store {store_id[:16]}… while "
                f"reporting total_pages={total}. A multi-page store cannot have "
                f"an empty first page, so this read is wrong — check the page "
                f"index convention rather than treating the dataset as absent."
            )

        last = total - 1 if first_page == 0 else total
        for page in range(first_page + 1, last + 1):
            # Do NOT swallow errors here: a mid-pagination failure must not look
            # like a complete (but truncated) key set.
            data = self._post("get_keys", {"id": store_id, "page": page})
            keys.extend(data.get("keys", []))
        return keys

    def get_root(self, store_id: str) -> dict:
        """Current on-chain root hash + confirmed status for a store.

        Typical response fields (Chia DataLayer RPC):
            success, hash (root hex), confirmed, timestamp

        ``confirmed`` flips True once the root-update spend lands on chain —
        which is how the writer tells a batch_update has actually been mined
        (T16 confirmation monitoring), not just accepted into the mempool.
        """
        return self._post("get_root", {"id": store_id})

    def get_proof(self, store_id: str, keys_hex: list[str]) -> dict:
        """Inclusion proof for one or more keys under the store root.

        ``keys`` are hex-encoded DataLayer keys (same form as batch_update).
        Used by orchard-verify live for SPEC §7 permanence/inclusion.

        NOTE: this is the one DataLayer endpoint whose store-ID parameter is
        named ``store_id`` rather than ``id`` — every other endpoint here uses
        ``id``. Sending ``id`` makes a real node ignore the store and the call
        fails. See docs/datalayer/reference/CHIA_DATALAYER_RPC.md §4.
        """
        return self._post(
            "get_proof",
            {"store_id": store_id, "keys": list(keys_hex)},
        )

    def verify_proof(
        self, coin_id: str, inner_puzzle_hash: str, store_proofs: dict
    ) -> dict:
        """Verify a ``get_proof`` result against the current on-chain root.

        Needs only a single root lookup — no store sync or subscription — so a
        stranger with any full node can run it. The response's ``current_root``
        (bool) is the meaningful bit: ``True`` ⇒ the proof chains to the
        currently published root. See docs/datalayer/reference/CHIA_DATALAYER_RPC.md §4.
        """
        return self._post(
            "verify_proof",
            {
                "coin_id": coin_id,
                "inner_puzzle_hash": inner_puzzle_hash,
                "store_proofs": store_proofs,
            },
        )
