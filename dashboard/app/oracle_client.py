# SPDX-License-Identifier: Apache-2.0
"""Thin HTTP client around the Oracle's REST API.

Used by Orchard View routes to talk to the oracle (running locally or
on the LAN). Keeps Flask routes free of `requests` boilerplate and
makes the oracle wire format easy to swap if it ever changes.
"""
from __future__ import annotations

import requests

from .config import settings


class OracleError(RuntimeError):
    """Raised for any non-2xx oracle response or transport failure."""


def _url(path: str) -> str:
    base = settings().oracle_url.rstrip("/")
    return f"{base}{path}"


def root() -> dict:
    try:
        r = requests.get(_url("/"), timeout=5)
    except requests.RequestException as e:
        raise OracleError(f"oracle unreachable: {e}") from e
    if r.status_code != 200:
        raise OracleError(f"oracle / returned {r.status_code}")
    return r.json()


def list_nodes() -> list[dict]:
    r = requests.get(_url("/nodes"), timeout=5)
    if r.status_code != 200:
        raise OracleError(f"GET /nodes -> {r.status_code}: {r.text}")
    return r.json()


def get_node(node_id: str) -> dict | None:
    r = requests.get(_url(f"/nodes/{node_id}"), timeout=5)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OracleError(f"GET /nodes/{node_id} -> {r.status_code}: {r.text}")
    return r.json()


def list_readings(node_id: str, limit: int = 50) -> list[dict]:
    r = requests.get(_url(f"/readings/{node_id}"), params={"limit": limit}, timeout=5)
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise OracleError(f"GET /readings/{node_id} -> {r.status_code}: {r.text}")
    return r.json()


def register_node(
    node_id: str,
    signing_key_hex: str,
    *,
    label: str | None = None,
    wallet_address: str | None = None,
    fw_version: str | None = None,
    authorization: str | None = None,
) -> dict:
    """POST /register on the oracle.

    Phase 6.6: ``authorization`` is the operator's session Bearer
    token (sourced from the browser's sessionStorage and forwarded
    by the dashboard's /api/oracle/register proxy). When set, the
    oracle uses session.address as the source of truth for the
    wallet binding; ``wallet_address`` from the body is optional and
    only echoed back as a sanity-check.
    """
    body: dict = {"node_id": node_id, "signing_key_hex": signing_key_hex}
    if label:
        body["label"] = label
    if wallet_address:
        body["wallet_address"] = wallet_address
    if fw_version:
        body["fw_version"] = fw_version
    headers: dict = {}
    if authorization:
        headers["Authorization"] = authorization
    r = requests.post(_url("/register"), json=body, headers=headers, timeout=5)
    if r.status_code not in (200, 201):
        raise OracleError(f"POST /register -> {r.status_code}: {r.text}")
    return r.json()


def get_uptime(node_id: str, season: int) -> dict | None:
    r = requests.get(_url(f"/uptime/{node_id}/{season}"), timeout=5)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OracleError(f"GET /uptime/{node_id}/{season} -> {r.status_code}: {r.text}")
    return r.json()


def latest_attestation(node_id: str) -> dict | None:
    """Phase 5.5 — returns the most recent on-chain attestation for
    the Tree, or None if no attestation has landed yet."""
    try:
        r = requests.get(_url(f"/attestations/{node_id}/latest"), timeout=5)
    except requests.RequestException as e:
        raise OracleError(f"GET /attestations/{node_id}/latest unreachable: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OracleError(
            f"GET /attestations/{node_id}/latest -> {r.status_code}: {r.text}")
    j = r.json()
    return j if j else None  # FastAPI may return null for None


def list_attestations(node_id: str, limit: int = 50) -> list[dict]:
    """All attestations on chain for the Tree, newest first."""
    try:
        r = requests.get(
            _url(f"/attestations/{node_id}"),
            params={"limit": limit},
            timeout=5,
        )
    except requests.RequestException as e:
        raise OracleError(f"GET /attestations/{node_id} unreachable: {e}") from e
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise OracleError(
            f"GET /attestations/{node_id} -> {r.status_code}: {r.text}")
    return r.json() or []


def network_stats() -> dict:
    """Phase 6.6 — aggregate network stats for the public-mode home
    card. Returns the parsed JSON dict from the oracle's
    /network/stats endpoint."""
    try:
        r = requests.get(_url("/network/stats"), timeout=5)
    except requests.RequestException as e:
        raise OracleError(f"GET /network/stats unreachable: {e}") from e
    if r.status_code != 200:
        raise OracleError(f"GET /network/stats -> {r.status_code}: {r.text}")
    return r.json()
