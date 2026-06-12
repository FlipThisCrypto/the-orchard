# SPDX-License-Identifier: Apache-2.0
"""HMAC-SHA256 signature verification for /readings.

The Tree firmware signs the request body with the per-Tree 32-byte
secret generated at first boot. The oracle stores the same secret
(captured at registration time over USB-serial via the dashboard)
and recomputes the HMAC to verify each submission.

If transport auth ever moves to the Tree's secp256r1 key (ADR-0007) —
today it signs dataset readings, not this HTTP envelope — only this
module and `Node.signing_key_hex` need to change; routes stay the same.
"""
from __future__ import annotations

import hmac
from hashlib import sha256

from sqlalchemy.orm import Session

from . import models


class SignatureError(Exception):
    """Raised for any failure to authenticate a submission."""


def verify_reading_sig(
    db: Session,
    node_id_header: str | None,
    sig_hex_header: str | None,
    body_bytes: bytes,
) -> models.Node:
    """Look up the Tree by its declared node_id, verify HMAC over body.

    Returns the Node ORM object on success. Raises SignatureError with
    a short reason on any failure (caller turns it into a 401/404).
    """
    if not node_id_header:
        raise SignatureError("missing X-Orchard-Node header")
    if not sig_hex_header:
        raise SignatureError("missing X-Orchard-Sig header")

    node = db.get(models.Node, node_id_header.upper())
    if node is None:
        raise SignatureError("unregistered node_id")

    try:
        secret = bytes.fromhex(node.signing_key_hex)
    except ValueError as e:
        raise SignatureError(f"stored signing key not valid hex: {e}") from e

    expected = hmac.new(secret, body_bytes, sha256).hexdigest()
    if not hmac.compare_digest(expected.lower(), sig_hex_header.lower()):
        raise SignatureError("signature mismatch")

    return node
