# SPDX-License-Identifier: Apache-2.0
"""AJAX endpoints used by the dashboard pages.

All under `/api/...`. Pure JSON in / JSON out. The HTML pages are
mostly static; these endpoints do the work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, request

from .. import oracle_client, tree_serial
from ..config import settings

bp = Blueprint("api", __name__)

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@bp.before_request
def _csrf_guard():
    """CSRF defense for state-changing /api calls.

    Browsers always send an Origin (and usually a Referer) on cross-origin
    requests; when present it must match this dashboard's own host.
    Requests with neither header are non-browser clients (curl, operator
    scripts) — not a CSRF vector — and are allowed. This matters because
    the serial + register routes reconfigure a connected Tree, and the
    dashboard's localhost bind is not a CSRF boundary (a malicious local
    page or DNS-rebinding can still reach 127.0.0.1)."""
    if request.method not in _UNSAFE_METHODS:
        return None
    for header in ("Origin", "Referer"):
        raw = request.headers.get(header)
        if raw:
            netloc = urlparse(raw).netloc
            if netloc and netloc != request.host:
                abort(403, description="cross-origin request rejected")
            return None  # the first header present is authoritative
    return None  # neither header => non-browser caller, allow


def _ok(data) -> tuple:
    return jsonify({"ok": True, **(data if isinstance(data, dict) else {"data": data})}), 200


def _err(msg: str, code: int = 400) -> tuple:
    return jsonify({"ok": False, "error": msg}), code


def _private(fn):
    """Mark a route as operator-only. Returns 404 in public mode so
    the route disappears from the surface area entirely — no hints
    to a public viewer that it exists. We use 404 (not 403) on
    purpose: the page is meant to be absent, not access-denied."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if settings().public_mode:
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- auth ----
#
# Phase 6.6. The dashboard's browser proxies the WalletConnect signed
# challenge to the oracle. The browser never talks to the oracle URL
# directly — keeps the same-origin posture and means we don't need
# CORS on the oracle. The Authorization: Bearer <session_token> is
# carried by all downstream /api/oracle/* requests after auth.

@bp.post("/auth/challenge")
def auth_challenge():
    """Forward to oracle /auth/challenge to get a single-use nonce."""
    import requests
    try:
        r = requests.post(
            f"{settings().oracle_url.rstrip('/')}/auth/challenge",
            timeout=5,
        )
    except requests.RequestException as e:
        return _err(f"oracle unreachable: {e}", code=502)
    return jsonify(r.json()), r.status_code


@bp.post("/auth/verify")
def auth_verify():
    """Forward signed-challenge payload to oracle /auth/verify."""
    import requests
    body = request.get_json(silent=True) or {}
    try:
        r = requests.post(
            f"{settings().oracle_url.rstrip('/')}/auth/verify",
            json=body, timeout=5,
        )
    except requests.RequestException as e:
        return _err(f"oracle unreachable: {e}", code=502)
    return jsonify(r.json()), r.status_code


@bp.get("/auth/config")
def auth_config():
    """What the browser needs to bootstrap WalletConnect.

    Operators set ORCHARD_VIEW_WC_PROJECT_ID in dashboard/.env. We
    expose just the project_id and a tiny metadata blob; everything
    else (chain id, method names) is hardcoded into the JS because
    CHIP-22 is the Chia spec.
    """
    # In public mode there are no operator actions to authenticate, so
    # don't expose the operator's WalletConnect project id — the browser
    # just sees "not configured" and hides the Connect button.
    project_id = "" if settings().public_mode else (settings().wc_project_id or "")
    return _ok({
        "wc_project_id": project_id,
        "wc_configured": bool(project_id),
        "metadata": {
            "name":        "Orchard View",
            "description": "Operator dashboard for The Orchard (Chia DePIN)",
            "url":         request.host_url.rstrip("/"),
            "icons":       [request.host_url.rstrip("/") +
                            "/static/img/icon.png"],
        },
    })


# ---------------------------------------------------------------- oracle ----

@bp.get("/oracle/status")
def oracle_status():
    try:
        info = oracle_client.root()
        return _ok({"oracle": info})
    except oracle_client.OracleError as e:
        return _err(str(e), code=502)


@bp.post("/oracle/verify_pass")
@_private
def oracle_verify_pass():
    """Check whether a Chia wallet holds at least one Orchard Pass.

    Hits the same MintGarden indexer the oracle's /register Pass gate
    uses, so a "verified here" answer is equivalent to a "would
    register here" answer (both go through the same cache layer in
    oracle.app.pass_verify). Returning the bound nft_id lets the
    wizard show the operator WHICH Pass it found, with a link to
    MintGarden.

    Gated behind @_private — verifying a public address is harmless,
    but the wizard step it backs is operator-only and the endpoint
    has no business being in public_mode.
    """
    body = request.get_json(silent=True) or {}
    address = (body.get("wallet_address") or "").strip()
    if not address:
        return _err("missing wallet_address", code=400)

    from orchard_chia.nft import verify as nft_verify
    try:
        passes = nft_verify.list_passes_by_address(address)
    except nft_verify.IndexerError as e:
        return _err(f"indexer error: {e}", code=502)

    if not passes:
        return _ok({
            "address":          address,
            "has_pass":         False,
            "pass_nft_id":      None,
            "pass_name":        None,
            "edition_number":   None,
            "buy_url":          (
                "https://mintgarden.io/collections/"
                "col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29"
            ),
        })

    # Bind the first Pass — matches the oracle's binding policy.
    first = passes[0]
    return _ok({
        "address":        address,
        "has_pass":       True,
        "pass_nft_id":    first.get("nft_coin_id"),
        "pass_name":      first.get("name"),
        "edition_number": first.get("edition_number"),
        "owner_count":    len(passes),
        "mintgarden_url": (
            f"https://mintgarden.io/nfts/{first.get('nft_coin_id')}"
        ),
    })


@bp.post("/oracle/register")
@_private
def oracle_register():
    """Proxy to oracle /register.

    Phase 6.6: the operator's wallet session token (from the browser's
    sessionStorage, attached as Authorization: Bearer by the wizard's
    authFetch wrapper) is forwarded to the oracle so /register can
    bind the Tree to the verified wallet. Without the forward, the
    oracle would 401 on require_wallet_session=True. The body's
    wallet_address is now optional and, when present, must match the
    session's verified address — the oracle enforces this.
    """
    body = request.get_json(silent=True) or {}
    auth = request.headers.get("Authorization")
    try:
        result = oracle_client.register_node(
            node_id=body["node_id"],
            signing_key_hex=body["signing_key_hex"],
            label=body.get("label"),
            wallet_address=body.get("wallet_address"),
            fw_version=body.get("fw_version"),
            authorization=auth,
        )
        return _ok({"register": result})
    except KeyError as e:
        return _err(f"missing field: {e.args[0]}", code=400)
    except oracle_client.OracleError as e:
        return _err(str(e), code=502)


# ---------------------------------------------------------------- serial ----

@bp.get("/serial/ports")
@_private
def serial_ports():
    ports = [{"device": p.device, "description": p.description, "hwid": p.hwid}
             for p in tree_serial.list_ports()]
    return _ok({"ports": ports})


@bp.post("/serial/identify")
@_private
def serial_identify():
    """Talk to a Tree, return its identity. Run after the user picks a port.

    Phase 9.0: also pulls the optional hardware fingerprint via
    HW_INFO. None when the connected Tree runs firmware <0.4.0 (or
    forgets to register the HW_INFO command). The wizard falls back
    to its pre-9.0 identify card in that case.
    """
    body = request.get_json(silent=True) or {}
    port = body.get("port")
    if not port:
        return _err("missing port", code=400)
    try:
        if not tree_serial.ping(port):
            return _err("no PING response — is this a Tree?", code=502)
        node_id = tree_serial.get_node_id(port)
        signing_key = tree_serial.get_signing_key(port)
        status = tree_serial.get_status(port)
        hw_info = tree_serial.get_hw_info(port)
        return _ok({
            "node_id": node_id,
            "signing_key_hex": signing_key,
            "status": status,
            "hw_info": hw_info,
        })
    except tree_serial.TreeError as e:
        return _err(str(e), code=502)


@bp.post("/serial/wifi")
@_private
def serial_wifi():
    body = request.get_json(silent=True) or {}
    port = body.get("port"); ssid = body.get("ssid"); password = body.get("password", "")
    if not port or not ssid:
        return _err("port and ssid required", code=400)
    try:
        tree_serial.set_wifi(port, ssid, password)
        return _ok({"wifi_set": True})
    except tree_serial.TreeError as e:
        return _err(str(e), code=502)


@bp.post("/serial/oracle")
@_private
def serial_set_oracle():
    body = request.get_json(silent=True) or {}
    port = body.get("port"); url = body.get("url")
    if not port or not url:
        return _err("port and url required", code=400)
    try:
        tree_serial.set_oracle_url(port, url)
        return _ok({"oracle_url_set": url})
    except tree_serial.TreeError as e:
        return _err(str(e), code=502)


@bp.post("/serial/sample")
@_private
def serial_sample_now():
    body = request.get_json(silent=True) or {}
    port = body.get("port")
    if not port:
        return _err("port required", code=400)
    try:
        tree_serial.sample_now(port)
        return _ok({"sampled": True})
    except tree_serial.TreeError as e:
        return _err(str(e), code=502)


# --------------------------------------------------------------- live view --

# ~111m precision — good enough to know the Tree is in the right region,
# bad enough that a public viewer can't pin it to a house.
PUBLIC_GPS_DECIMALS = 3


def _coarsen_coord(v):
    if v is None:
        return None
    try:
        return round(float(v), PUBLIC_GPS_DECIMALS)
    except (TypeError, ValueError):
        return v


def _scrub_reading_for_public(r: dict) -> dict:
    """Return a copy of a reading with operator-sensitive fields removed
    or coarsened. Used when ``settings().public_mode`` is True so that
    /api/tree/<id>/latest can be safely exposed via a public tunnel.

    Scrubs:
      - ``gps_lat`` / ``gps_lon``           (top-level)
      - ``payload.sensors.gps.{lat,lon}``   (nested, firmware-native form)
    """
    out = dict(r)
    out["gps_lat"] = _coarsen_coord(out.get("gps_lat"))
    out["gps_lon"] = _coarsen_coord(out.get("gps_lon"))
    payload = out.get("payload")
    if isinstance(payload, dict):
        sensors = payload.get("sensors")
        if isinstance(sensors, dict):
            gps = sensors.get("gps")
            if isinstance(gps, dict):
                new_gps = dict(gps)
                for k in ("lat", "lon", "latitude", "longitude"):
                    if k in new_gps:
                        new_gps[k] = _coarsen_coord(new_gps[k])
                new_sensors = dict(sensors)
                new_sensors["gps"] = new_gps
                new_payload = dict(payload)
                new_payload["sensors"] = new_sensors
                out["payload"] = new_payload
    return out


def _scrub_node_for_public(n: dict) -> dict:
    """Return a copy of a node record with operator-private fields
    removed. Used in public mode. ``wallet_address`` couples Tree
    location to a payout address — leaks doxx + financial linkage."""
    out = dict(n)
    out.pop("wallet_address", None)
    return out


@bp.get("/tree/<node_id>/latest")
def tree_latest(node_id: str):
    node_id = node_id.upper()
    try:
        node = oracle_client.get_node(node_id)
        if node is None:
            return _err("unknown node_id", code=404)
        readings = oracle_client.list_readings(node_id, limit=20)

        # In public-demo mode, strip operator-private fields BEFORE any
        # of the response derivations look at them, so we never leak
        # them via `latest`, `node`, or the readings list.
        if settings().public_mode:
            node = _scrub_node_for_public(node)
            readings = [_scrub_reading_for_public(r) for r in readings]

        current_season = None
        uptime = None
        try:
            current_season = oracle_client.root().get("current_season")
            if current_season:
                uptime = oracle_client.get_uptime(node_id, current_season)
        except oracle_client.OracleError:
            pass  # uptime is nice-to-have; never break the live view for it

        # Compute "alive" heuristic: last reading within 2x sample interval.
        alive = False
        last_received_at = None
        if readings:
            last_received_at = readings[0].get("received_at")
            try:
                iso = last_received_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso)
                # Oracle stores naive UTC strings; treat any missing tz
                # as UTC rather than letting aware-vs-naive subtract
                # raise and silently force `alive=False` (which would
                # mark fresh-but-naive timestamps "Stale" forever).
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                alive = age < 180  # default sample interval is 60s; 3x for tolerance
            except (TypeError, ValueError, AttributeError):
                pass

        # Phase 5.5: latest on-chain attestation for the "On chain"
        # card. Nice-to-have; never break the live view if it fails.
        attestation = None
        try:
            attestation = oracle_client.latest_attestation(node_id)
        except oracle_client.OracleError:
            pass

        return _ok({
            "node": node,
            "readings": readings,
            "latest": readings[0] if readings else None,
            "alive": alive,
            "last_received_at": last_received_at,
            "current_season": current_season,
            "uptime": uptime,
            "attestation": attestation,
            "server_now_utc": datetime.now(timezone.utc).isoformat(),
        })
    except oracle_client.OracleError as e:
        return _err(str(e), code=502)
