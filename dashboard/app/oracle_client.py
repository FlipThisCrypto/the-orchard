# SPDX-License-Identifier: Apache-2.0
"""Thin HTTP client around the Oracle's REST API.

Used by Orchard View routes to talk to the oracle (running locally or
on the LAN). Keeps Flask routes free of `requests` boilerplate and
makes the oracle wire format easy to swap if it ever changes.

Every public function converts transport and parse failures into
``OracleError`` so routes can degrade gracefully (empty fleet / banner)
instead of 500'ing when the oracle is down, returns HTML, or returns
an empty body.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import settings


class OracleError(RuntimeError):
    """Raised for any non-2xx oracle response or transport/parse failure."""


def _url(path: str) -> str:
    base = settings().oracle_url.rstrip("/")
    return f"{base}{path}"


def _request_json(
    method: str,
    path: str,
    *,
    ok_statuses: tuple[int, ...] = (200,),
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 5,
) -> Any:
    """HTTP call that always raises OracleError on failure (never JSONDecodeError)."""
    try:
        r = requests.request(
            method,
            _url(path),
            params=params,
            json=json_body,
            headers=headers or {},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise OracleError(f"oracle {method} {path} unreachable: {e}") from e

    if r.status_code not in ok_statuses:
        # Truncate body so logs/UI don't get a full HTML error page.
        snippet = (r.text or "")[:200]
        raise OracleError(
            f"oracle {method} {path} -> {r.status_code}: {snippet}"
        )

    # Empty body with 200/201 is treated as null/empty, not a crash.
    if not (r.content or b"").strip():
        return None
    try:
        return r.json()
    except ValueError as e:
        # Includes JSONDecodeError — empty, HTML, or truncated proxy body.
        raise OracleError(
            f"oracle {method} {path} returned non-JSON body "
            f"({len(r.content or b'')} bytes): {e}"
        ) from e


def root() -> dict:
    data = _request_json("GET", "/")
    if not isinstance(data, dict):
        raise OracleError(
            "oracle GET / returned non-JSON object "
            f"(got {type(data).__name__})"
        )
    return data


def list_nodes() -> list[dict]:
    data = _request_json("GET", "/nodes")
    if data is None:
        return []
    if not isinstance(data, list):
        raise OracleError("oracle GET /nodes returned non-list JSON")
    return data


def get_node(node_id: str) -> dict | None:
    try:
        data = _request_json("GET", f"/nodes/{node_id}")
    except OracleError as e:
        if "-> 404" in str(e):
            return None
        raise
    return data if isinstance(data, dict) else None


def list_readings(node_id: str, limit: int = 50) -> list[dict]:
    try:
        data = _request_json(
            "GET",
            f"/readings/{node_id}",
            params={"limit": limit},
        )
    except OracleError as e:
        if "-> 404" in str(e):
            return []
        raise
    if data is None:
        return []
    if not isinstance(data, list):
        raise OracleError(f"oracle GET /readings/{node_id} returned non-list")
    return data


def register_node(
    node_id: str,
    signing_key_hex: str,
    *,
    label: str | None = None,
    wallet_address: str | None = None,
    fw_version: str | None = None,
    device_pubkey: str | None = None,
    authorization: str | None = None,
) -> dict:
    """POST /register on the oracle.

    Phase 6.6: ``authorization`` is the operator's session Bearer
    token (sourced from the browser's sessionStorage and forwarded
    by the dashboard's /api/oracle/register proxy). When set, the
    oracle uses session.address as the source of truth for the
    wallet binding; ``wallet_address`` from the body is optional and
    only echoed back as a sanity-check.

    ADR-0003: ``device_pubkey`` is the Tree's compressed secp256r1
    public key (66 hex). When provided, the oracle stores it and the
    DataLayer publisher writes it into ``node:<id>.pubkey``.
    """
    body: dict = {"node_id": node_id, "signing_key_hex": signing_key_hex}
    if label:
        body["label"] = label
    if wallet_address:
        body["wallet_address"] = wallet_address
    if fw_version:
        body["fw_version"] = fw_version
    if device_pubkey:
        body["device_pubkey"] = device_pubkey
    headers: dict = {}
    if authorization:
        headers["Authorization"] = authorization
    data = _request_json(
        "POST",
        "/register",
        ok_statuses=(200, 201),
        json_body=body,
        headers=headers,
    )
    if not isinstance(data, dict):
        raise OracleError("oracle POST /register returned non-object JSON")
    return data


def get_uptime(node_id: str, season: int) -> dict | None:
    try:
        data = _request_json("GET", f"/uptime/{node_id}/{season}")
    except OracleError as e:
        if "-> 404" in str(e):
            return None
        raise
    return data if isinstance(data, dict) else None


def latest_attestation(node_id: str) -> dict | None:
    """Phase 5.5 — returns the most recent on-chain attestation for
    the Tree, or None if no attestation has landed yet."""
    try:
        data = _request_json("GET", f"/attestations/{node_id}/latest")
    except OracleError as e:
        if "-> 404" in str(e):
            return None
        raise
    return data if isinstance(data, dict) else None


def list_attestations(node_id: str, limit: int = 50) -> list[dict]:
    """All attestations on chain for the Tree, newest first."""
    try:
        data = _request_json(
            "GET",
            f"/attestations/{node_id}",
            params={"limit": limit},
        )
    except OracleError as e:
        if "-> 404" in str(e):
            return []
        raise
    if data is None:
        return []
    if not isinstance(data, list):
        raise OracleError(
            f"oracle GET /attestations/{node_id} returned non-list"
        )
    return data


def network_stats() -> dict:
    """Phase 6.6 — aggregate network stats for the public-mode home
    card. Returns the parsed JSON dict from the oracle's
    /network/stats endpoint."""
    data = _request_json("GET", "/network/stats")
    if not isinstance(data, dict):
        raise OracleError("oracle GET /network/stats returned non-object JSON")
    return data
