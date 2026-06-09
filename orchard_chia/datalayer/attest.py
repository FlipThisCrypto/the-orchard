# SPDX-License-Identifier: Apache-2.0
"""Build, sign, and (de)serialize Season attestation records.

Pure functions only — no I/O. The orchestrator (`main.py`) wires
these together with the oracle client and the DataLayer RPC client.

**Wire format** (Phase 5):

  Key   = utf-8 bytes of  ``attest:<NODE_ID>:<SEASON:08d>``
          example:        ``attest:5B9BB0...:00000002``

  Value = utf-8 bytes of canonical JSON of the signed attestation:
          {
            "node_id":              "<32 hex>",
            "season":               <int>,
            "season_start_utc":     "<ISO 8601>",
            "season_end_utc":       "<ISO 8601>",
            "hours_online":         <0..24>,
            "block_height_at_write":<int>,
            "data_hash":            "<sha256 hex>",
            "signed_at":            "<ISO 8601 UTC>",
            "oracle_sig":           "<HMAC-SHA256 hex>"
          }

  Both key and value are hex-encoded when handed to DataLayer's
  ``batch_update`` call.

**Canonicalization rule for signing**: JSON dump with ``sort_keys=True``,
no spaces (``separators=(",", ":")``), then UTF-8 encode. The
``oracle_sig`` field is removed before computing the signature and
added afterward.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from hashlib import sha256

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_signing_key(signing_key_hex: str) -> bytes:
    """Guard: a signing key must be exactly 64 hex chars (32 bytes).

    Without this, an empty or malformed key would silently HMAC under a
    short/empty key — a signature an attacker could reproduce. Fail loud
    instead (the payout verifier relies on this being a real secret)."""
    if not isinstance(signing_key_hex, str) or not _HEX64.match(signing_key_hex):
        raise ValueError("signing_key_hex must be 64 hex characters (32 bytes)")
    return bytes.fromhex(signing_key_hex)


def build_attestation_payload(
    *,
    node_id: str,
    season: int,
    hours_online: int,
    season_start_utc: str,
    season_end_utc: str,
    block_height_at_write: int,
    data_hash: str,
    signed_at: datetime | None = None,
) -> dict:
    """Construct the unsigned attestation body. Pure function."""
    if signed_at is None:
        signed_at = datetime.now(timezone.utc)
    return {
        "node_id": node_id.upper(),
        "season": int(season),
        "season_start_utc": season_start_utc,
        "season_end_utc": season_end_utc,
        "hours_online": int(hours_online),
        "block_height_at_write": int(block_height_at_write),
        "data_hash": data_hash,
        "signed_at": signed_at.isoformat(),
    }


def _canonical_bytes(payload_without_sig: dict) -> bytes:
    body = {k: v for k, v in payload_without_sig.items() if k != "oracle_sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict, signing_key_hex: str) -> dict:
    """Return ``payload`` plus an ``oracle_sig`` HMAC-SHA256 over the
    canonical bytes. Caller may then hex-encode and ship to DataLayer.
    """
    key = _require_signing_key(signing_key_hex)
    canonical = _canonical_bytes(payload)
    sig = hmac.new(key, canonical, sha256).hexdigest()
    return {**payload, "oracle_sig": sig.upper()}


def verify_signature(signed_payload: dict, signing_key_hex: str) -> bool:
    """Constant-time check that ``oracle_sig`` is the HMAC over the rest
    of the fields. Used by future Keeper-class validators.
    """
    key = _require_signing_key(signing_key_hex)
    provided = signed_payload.get("oracle_sig") or ""
    canonical = _canonical_bytes(signed_payload)
    expected = hmac.new(key, canonical, sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), provided.lower())


def data_hash_for_uptime(node_id: str, season: int, hours_online: int) -> str:
    """v1 placeholder — hash of the salient identifiers. In v1.1 this
    becomes a Merkle root over the per-hour reading buckets, so a Keeper
    can request the underlying data and re-verify."""
    return hashlib.sha256(
        f"{node_id.upper()}:{int(season)}:{int(hours_online)}".encode("utf-8")
    ).hexdigest()


def datalayer_key_for(node_id: str, season: int) -> str:
    """Hex-encoded UTF-8 of `attest:<node>:<season:08d>`."""
    raw = f"attest:{node_id.upper()}:{int(season):08d}".encode("utf-8")
    return raw.hex()


def datalayer_value_for(signed_payload: dict) -> str:
    """Hex-encoded UTF-8 canonical JSON of the signed payload."""
    canonical = json.dumps(
        signed_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return canonical.hex()


def parse_datalayer_value(value_hex: str | None) -> dict | None:
    """Inverse of `datalayer_value_for` — reads back from DataLayer."""
    if not value_hex:
        return None
    try:
        return json.loads(bytes.fromhex(value_hex).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
