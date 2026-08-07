# SPDX-License-Identifier: Apache-2.0
"""Thin HTTP client to The Orchard's oracle service.

Used by the attestation writer to find registered Trees and their
per-Season uptime. Same wire format as the dashboard's
``oracle_client.py`` — kept intentionally separate so each component
can vendor its own minimal client.
"""
from __future__ import annotations

import requests


class OracleError(RuntimeError):
    """Raised for any non-2xx oracle response or transport failure."""


class OracleClient:
    """Read client for the oracle.

    ``writer_token`` authenticates this process as the operator's own DataLayer
    writer / payout job (the same ``X-Orchard-Writer-Token`` POST /attestations
    uses). It is required to read operator-private fields — notably
    ``wallet_address``, which the payout needs to know where to send $JUICE.
    When the job runs on the oracle host the token may be omitted: the oracle
    falls back to loopback-only trust.
    """

    def __init__(self, base_url: str, writer_token: str | None = None):
        self.base = base_url.rstrip("/")
        self._writer_token = (writer_token or "").strip()

    def _auth_headers(self) -> dict:
        if self._writer_token:
            return {"X-Orchard-Writer-Token": self._writer_token}
        return {}

    def _get(self, path: str, **kwargs) -> dict | list:
        headers = {**self._auth_headers(), **(kwargs.pop("headers", None) or {})}
        try:
            r = requests.get(f"{self.base}{path}", timeout=10, headers=headers, **kwargs)
        except requests.RequestException as e:
            raise OracleError(f"oracle unreachable: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise OracleError(f"GET {path} -> {r.status_code}: {r.text}")
        # HTTP 200 is not a promise of JSON. A proxy landing page, a Cloudflare
        # challenge, or an empty body all arrive as 200 with a body json()
        # cannot parse — and this call used to sit OUTSIDE the try, so the
        # resulting JSONDecodeError was never an OracleError, sailed past every
        # `except OracleError`, and killed the process with a traceback instead
        # of the intended exit 3. That is exactly how the 2026-07-26 publish and
        # attest runs died in under 35 ms, against a URL answering 200 with HTML.
        try:
            return r.json()
        except ValueError as e:
            body = (r.text or "")[:200].replace("\n", " ")
            raise OracleError(
                f"GET {path} -> 200 but the body is not JSON ({e}). "
                f"Is {self.base} really the oracle? First 200 bytes: {body!r}"
            ) from e

    def root(self) -> dict:
        info = self._get("/")
        if not isinstance(info, dict):
            raise OracleError("oracle root returned no JSON object (404 or bad body)")
        return info

    def current_season(self) -> int:
        info = self.root()
        try:
            return int(info["current_season"])
        except (KeyError, TypeError, ValueError) as e:
            # Turn a malformed oracle payload into OracleError so callers'
            # existing `except OracleError` handles it instead of crashing.
            raise OracleError(
                f"oracle root missing/invalid current_season: {e}"
            ) from e

    def list_nodes(self) -> list[dict]:
        return self._get("/nodes") or []

    def get_uptime(self, node_id: str, season: int) -> dict | None:
        return self._get(f"/uptime/{node_id}/{season}")

    def get_node(self, node_id: str) -> dict | None:
        """Single node record (incl. wallet_address). None on 404.
        Used by the payout to resolve each Tree's recipient wallet."""
        return self._get(f"/nodes/{node_id}")

    def get_readings(
        self,
        node_id: str,
        limit: int = 500,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[dict]:
        """Recent readings for a Tree (newest first). Used by the hot-path
        DataLayer publisher to harvest device-signed SPEC readings.

        Optional ``since_ms`` / ``until_ms`` filter on ``tree_ts_ms``
        (inclusive / exclusive) so a closed UTC hour can be fetched without
        scanning full history. Returns [] on 404 / empty.
        """
        limit = max(1, min(int(limit), 2000))
        params: dict = {"limit": limit}
        if since_ms is not None:
            params["since_ms"] = int(since_ms)
        if until_ms is not None:
            params["until_ms"] = int(until_ms)
        data = self._get(f"/readings/{node_id.upper()}", params=params)
        if data is None:
            return []
        if not isinstance(data, list):
            raise OracleError(f"GET /readings/{node_id} returned non-list")
        return data
