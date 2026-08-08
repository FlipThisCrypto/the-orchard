# SPDX-License-Identifier: Apache-2.0
"""Chia block-hash beacon for Tree anti-backdating (SPEC §4.2).

Trees fetch a recent header hash prefix before signing each reading as
``block_anchor``. v1 sources this from the operator's full node when
configured; otherwise returns a zeroed placeholder so the signing path
still works offline (verifiers will treat zero anchors as non-binding).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger("orchard.oracle.beacon")

# Optional override for tests / offline demos (16+ hex chars).
_ENV_ANCHOR = "ORCHARD_BEACON_BLOCK_ANCHOR"
_ENV_HEIGHT = "ORCHARD_BEACON_BLOCK_HEIGHT"

# In-process cache: Trees poll /beacon every sample; full-node RPC is
# expensive and anchors only need ~block-time freshness. Default 60s.
_CACHE_TTL_S = float(os.environ.get("ORCHARD_BEACON_CACHE_TTL_S", "60") or "60")
_cache: dict | None = None
_cache_mono: float = 0.0


def _env_beacon() -> dict | None:
    anchor = (os.environ.get(_ENV_ANCHOR) or "").strip().lower()
    if not anchor or len(anchor) < 16:
        return None
    height_raw = (os.environ.get(_ENV_HEIGHT) or "0").strip()
    try:
        height = int(height_raw)
    except ValueError:
        height = 0
    return {
        "block_height": height,
        "block_hash": anchor if len(anchor) >= 64 else anchor,
        "block_anchor": anchor[:16],
        "source": "env",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
    }


def _fullnode_beacon() -> dict | None:
    """Best-effort peek at a local full node (same cert paths as chia config).

    Soft-fails: missing certs / offline node → None (caller uses placeholder).
    """
    host = os.environ.get("ORCHARD_FULLNODE_HOST", "127.0.0.1")
    port = int(os.environ.get("ORCHARD_FULLNODE_PORT", "8555"))
    cert = os.environ.get("ORCHARD_FULLNODE_CERT", "")
    key = os.environ.get("ORCHARD_FULLNODE_KEY", "")
    if not cert or not key:
        return None
    if not os.path.isfile(cert) or not os.path.isfile(key):
        return None
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(
            f"https://{host}:{port}/get_blockchain_state",
            json={},
            cert=(cert, key),
            verify=False,
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        peak = (data.get("blockchain_state") or {}).get("peak") or {}
        header_hash = (peak.get("header_hash") or "").lower()
        height = int(peak.get("height") or 0)
        if not header_hash:
            return None
        # header_hash may be 0x-prefixed 64-hex.
        hh = header_hash[2:] if header_hash.startswith("0x") else header_hash
        return {
            "block_height": height,
            "block_hash": hh,
            "block_anchor": hh[:16],
            "source": "full_node",
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # noqa: BLE001 — soft beacon path
        log.debug("full node beacon unavailable: %s", e)
        return None


def _fresh_beacon() -> dict:
    for loader in (_env_beacon, _fullnode_beacon):
        got = loader()
        if got is not None:
            return {"ok": True, **got}
    return {
        "ok": False,
        "block_height": 0,
        "block_hash": "0" * 64,
        "block_anchor": "0" * 16,
        "source": "placeholder",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "detail": (
            "No beacon source configured. Set ORCHARD_BEACON_BLOCK_ANCHOR "
            "or ORCHARD_FULLNODE_CERT/KEY for a live header."
        ),
    }


@router.get("/beacon")
def beacon() -> dict:
    """Recent Chia header prefix for Tree ``block_anchor`` (SPEC §4.2)."""
    import time
    global _cache, _cache_mono
    now = time.monotonic()
    if (
        _cache is not None
        and _CACHE_TTL_S > 0
        and (now - _cache_mono) < _CACHE_TTL_S
    ):
        out = dict(_cache)
        out["cached"] = True
        return out
    body = _fresh_beacon()
    _cache = dict(body)
    _cache_mono = now
    body = dict(body)
    body["cached"] = False
    return body
