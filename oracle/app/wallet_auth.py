# SPDX-License-Identifier: Apache-2.0
"""Chia wallet signature verification (Phase 6.6).

Verifies that a wallet that holds a given ``xch1...`` address actually
signed a challenge. The verification is two-step:

  1. The submitted BLS signature is valid over the message under the
     submitted public key. Proves: "someone with this pk signed this."
  2. The submitted public key DERIVES the submitted address. Proves:
     "this pk is the operator behind this address."

Without step (2), an attacker could submit *their* pubkey + signature
for *someone else's* address and the oracle would happily accept it
("yes, someone signed something") — the whole point of asking for the
signature would be defeated. Both checks must pass.

Chia WalletConnect (CHIP-22) ``chia_signMessageByAddress`` returns:

  {
    "publicKey": "<48-byte G1 element, hex>",  # synthetic pk of the address
    "signature": "<96-byte G2 element, hex>",  # AugScheme over the message
  }

The message that's signed is the *raw* challenge string. (Sage and
Goby both pre-pend ``Chia Signed Message:\\n`` before passing to
AugScheme.sign(); we mirror that here on verify.)

Wallet-pk → xch1-address derivation uses the standard p2 delegated
puzzle: the address's puzzle hash is
``curry_and_treehash(P2_TEMPLATE_HASH, treehash(pk))``, bech32m-encoded
with HRP ``xch`` for mainnet. The wallet returns the *synthetic* pk
directly, so no synthetic-offset derivation step is needed on our side.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

# The standard "p2 delegated puzzle or hidden puzzle" template hash.
# Public constant from the Chia codebase; do not change.
P2_DELEGATED_TEMPLATE_HASH: Final[bytes] = bytes.fromhex(
    "e9aaa49f45bad5c889b86ee3341550c155cfdd10c3a6757de618d20612fffd52"
)

# Prefix Chia wallets prepend to a signMessageByAddress payload before
# BLS-signing. Documented in CHIP-22.
SIGNED_MESSAGE_PREFIX: Final[str] = "Chia Signed Message:\n"


class WalletAuthError(RuntimeError):
    """Raised when verification could not be performed (bad inputs,
    library not available, malformed bech32, etc.). Distinct from
    "signature was valid but failed verification" — callers usually
    log auth errors loud and reject silently."""


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""    # human-readable failure reason for logs


# ----------------------------------------------------------------------
# Low-level helpers — bech32m + tree hashing.
# ----------------------------------------------------------------------

# bech32m alphabet + checksum constants (BIP-350). XCH address uses
# the bech32m variant, not the older bech32; the only difference is
# the checksum constant.
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONST = 0x2bc830a3


def _bech32_polymod(values: list[int]) -> int:
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == _BECH32M_CONST


def _bech32_decode(addr: str) -> tuple[str, bytes] | None:
    """Decode a bech32m string. Returns (hrp, data_bytes) or None on
    malformed input. data_bytes is the decoded 32-byte payload."""
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return None
    if addr.lower() != addr and addr.upper() != addr:
        return None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        return None
    hrp = addr[:pos]
    data: list[int] = []
    for c in addr[pos + 1:]:
        if c not in _BECH32_CHARSET:
            return None
        data.append(_BECH32_CHARSET.index(c))
    if not _bech32_verify_checksum(hrp, data):
        return None
    # Convert from 5-bit groups to 8-bit bytes (drop the 6-byte checksum).
    payload5 = data[:-6]
    acc = 0
    bits = 0
    out = bytearray()
    for v in payload5:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    return hrp, bytes(out)


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _atom_tree_hash(atom: bytes) -> bytes:
    """The "tree hash" of a CLVM atom is sha256(0x01 || atom)."""
    return _sha256(b"\x01" + atom)


def _curry_one_pk_treehash(template_hash: bytes, pk_bytes: bytes) -> bytes:
    """Compute the puzzle hash of (currying ``pk_bytes`` into the
    template puzzle whose hash is ``template_hash``).

    This is the standard Chia ``curry_and_treehash`` for a single 48-byte
    pubkey arg. The formula reduces to:

      pair = ph_pair(treehash(pk_atom), treehash_of_empty_list)
      return ph_pair(template_hash, pair)

    where ph_pair(l, r) = sha256(0x02 || l || r). For the single-arg
    curry case the right side of the inner pair is the "treehash of
    an empty environment" which is the well-known constant below.
    """
    PH_NIL = _atom_tree_hash(b"")   # treehash of `()` atom

    # Build the curried-environment treehash bottom-up.
    pk_ph = _atom_tree_hash(pk_bytes)
    inner = _sha256(b"\x02" + pk_ph + PH_NIL)
    return _sha256(b"\x02" + template_hash + inner)


def puzzle_hash_for_synthetic_pk(synth_pk_bytes: bytes) -> bytes:
    """Standard p2_delegated puzzle hash for a synthetic pubkey.

    The CHIP-22 signMessageByAddress flow returns the synthetic pk
    directly, so this is the value the wallet's address derives from.
    """
    if len(synth_pk_bytes) != 48:
        raise WalletAuthError(
            f"synthetic public key must be 48 bytes, got {len(synth_pk_bytes)}")
    return _curry_one_pk_treehash(P2_DELEGATED_TEMPLATE_HASH, synth_pk_bytes)


def xch_address_for_synthetic_pk(synth_pk_bytes: bytes,
                                 *, hrp: str = "xch") -> str:
    """Derive the bech32m-encoded address from a synthetic pubkey."""
    ph = puzzle_hash_for_synthetic_pk(synth_pk_bytes)
    return _encode_bech32m(hrp, ph)


def _encode_bech32m(hrp: str, data: bytes) -> str:
    """Bech32m-encode 32-byte payload with the given HRP. Used as the
    inverse of _bech32_decode for sanity-checking address derivation
    in tests."""
    # Convert 8-bit bytes to 5-bit groups.
    acc = 0
    bits = 0
    out: list[int] = []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits > 0:
        out.append((acc << (5 - bits)) & 31)
    # Compute checksum.
    values = _bech32_hrp_expand(hrp) + out + [0] * 6
    polymod = _bech32_polymod(values) ^ _BECH32M_CONST
    chk = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[i] for i in out + chk)


# ----------------------------------------------------------------------
# Public verify entry point.
# ----------------------------------------------------------------------

def verify_chia_signed_message(
    *,
    address: str,
    public_key_hex: str,
    signature_hex: str,
    message: str,
    test_mode: bool = False,
) -> VerifyResult:
    """Verify a CHIP-22 chia_signMessageByAddress payload.

    Returns VerifyResult(ok=True) only when BOTH:
      - The BLS signature is valid for (pubkey, "Chia Signed Message:\\n" + message).
      - The pubkey derives to the submitted xch1 address.

    ``test_mode=True`` skips the BLS verify (still does the
    pubkey→address check) so the auth flow can be tested without a
    real wallet. Production callers MUST NOT pass test_mode=True.
    """
    # 1) parse address -> puzzle_hash
    dec = _bech32_decode(address)
    if dec is None:
        return VerifyResult(False, "address: malformed bech32m")
    hrp, claimed_ph = dec
    if hrp not in ("xch", "txch"):
        return VerifyResult(False, f"address: unexpected HRP {hrp!r}")
    if len(claimed_ph) != 32:
        return VerifyResult(False, f"address: payload is {len(claimed_ph)} bytes, want 32")

    # 2) parse pubkey, derive its puzzle hash, compare to address's
    try:
        pk_bytes = bytes.fromhex(public_key_hex.removeprefix("0x"))
    except ValueError:
        return VerifyResult(False, "public_key_hex: not valid hex")
    try:
        derived_ph = puzzle_hash_for_synthetic_pk(pk_bytes)
    except WalletAuthError as e:
        return VerifyResult(False, f"public_key: {e}")
    if derived_ph != claimed_ph:
        return VerifyResult(
            False,
            "public_key does NOT derive to address (pk-binding check failed)",
        )

    # 3) BLS verify the signature over the prefixed message.
    if test_mode:
        return VerifyResult(True, "test_mode bypass (BLS verify skipped)")

    try:
        sig_bytes = bytes.fromhex(signature_hex.removeprefix("0x"))
    except ValueError:
        return VerifyResult(False, "signature_hex: not valid hex")

    prefixed = (SIGNED_MESSAGE_PREFIX + message).encode("utf-8")
    try:
        # Imported lazily so the module is importable in test
        # environments that don't have chia-rs installed.
        from chia_rs import G1Element, G2Element, AugSchemeMPL
    except ImportError as e:
        raise WalletAuthError(
            "chia-rs not installed; cannot verify BLS signature"
        ) from e

    try:
        pk = G1Element.from_bytes(pk_bytes)
        sig = G2Element.from_bytes(sig_bytes)
    except Exception as e:
        return VerifyResult(False, f"BLS element parse failed: {e}")

    if not AugSchemeMPL.verify(pk, prefixed, sig):
        return VerifyResult(False, "BLS signature verification failed")

    return VerifyResult(True)
