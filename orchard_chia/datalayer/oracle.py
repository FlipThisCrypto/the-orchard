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
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _get(self, path: str, **kwargs) -> dict | list:
        try:
            r = requests.get(f"{self.base}{path}", timeout=10, **kwargs)
        except requests.RequestException as e:
            raise OracleError(f"oracle unreachable: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise OracleError(f"GET {path} -> {r.status_code}: {r.text}")
        return r.json()

    def root(self) -> dict:
        return self._get("/")

    def current_season(self) -> int:
        info = self.root()
        return int(info["current_season"])

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
