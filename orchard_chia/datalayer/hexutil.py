# SPDX-License-Identifier: Apache-2.0
"""Hex helpers for DataLayer keys/values."""
from __future__ import annotations


def is_hex(s: str, *, length: int | None = None) -> bool:
    if not isinstance(s, str) or (len(s) % 2) != 0:
        return False
    if length is not None and len(s) != length:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def is_compressed_p256_pubkey(s: str) -> bool:
    if not is_hex(s, length=66):
        return False
    return s.lower()[:2] in ("02", "03")
