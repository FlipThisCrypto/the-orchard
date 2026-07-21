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
from dataclasses import dataclass, field
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

    def batch_update(self, store_id: str, changelist: list[dict]) -> dict:
        """Apply a list of insert/delete operations to a DataLayer store.

        ``changelist`` items look like:
            {"action": "insert", "key": "<hex>", "value": "<hex>"}
            {"action": "delete", "key": "<hex>"}
        """
        body = {"id": store_id, "changelist": changelist}
        return self._post("batch_update", body)

    def get_value(self, store_id: str, key_hex: str) -> str | None:
        body = {"id": store_id, "key": key_hex}
        try:
            data = self._post("get_value", body)
        except ChiaRpcError:
            return None
        return data.get("value")

    def get_keys(self, store_id: str) -> list[str]:
        """All keys in the store, hex-encoded. Used by the payout
        reader to discover every attestation that's been published."""
        body = {"id": store_id}
        try:
            data = self._post("get_keys", body)
        except ChiaRpcError:
            return []
        return data.get("keys", [])

    def get_root(self, store_id: str) -> dict:
        """Current on-chain root hash + confirmed status for a store.

        Typical response fields (Chia DataLayer RPC):
            success, hash (root hex), confirmed, timestamp
        """
        return self._post("get_root", {"id": store_id})

    def get_proof(self, store_id: str, keys_hex: list[str]) -> dict:
        """Inclusion proof for one or more keys under the store root.

        ``keys`` are hex-encoded DataLayer keys (same form as batch_update).
        Used by orchard-verify live for SPEC §7 permanence/inclusion.
        """
        return self._post(
            "get_proof",
            {"id": store_id, "keys": list(keys_hex)},
        )
