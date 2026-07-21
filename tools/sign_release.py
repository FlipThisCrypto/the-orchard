# SPDX-License-Identifier: Apache-2.0
"""Sign Orchard firmware release binaries (T7 / signed OTA).

Produces a detached 64-byte P-256 ECDSA signature (r‖s, low-S, SHA-256 of
the raw image bytes) next to each ``.bin`` as ``<name>.sig``, and optionally
appends ``.sig`` digests to ``SHA256SUMS.txt``.

The release private key must **never** live in the repo. Pass it via:

* ``--key-env OTA_SIGNING_KEY`` (GitHub Actions secret contents), or
* ``--key-file path.pem`` (local offline only; path must be gitignored)

If the key is missing and ``--require`` is not set, exit 0 with a clear
``UNSIGNED`` notice so forks / pre-secret CI still succeed (warn-mode
rollout). With ``--require``, exit 2 so a production release cannot ship
unsigned by accident.

Usage (from repo root)::

    python tools/sign_release.py --key-env OTA_SIGNING_KEY dist/*.bin
    python tools/sign_release.py --key-file /secure/orchard-ota-release.key \\
        --sums dist/SHA256SUMS.txt dist/*.bin
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path


def _load_signing_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("key is not an EC private key")
    if key.curve.name not in ("secp256r1", "prime256v1"):
        # cryptography may report "secp256r1"
        if getattr(key.curve, "key_size", None) != 256:
            raise ValueError(f"expected P-256 key, got curve {key.curve.name!r}")
    return key


def _low_s_rs(signature_der: bytes, order: int) -> bytes:
    """Convert DER ECDSA sig to 64-byte r‖s with low-S normalization."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    r, s = decode_dss_signature(signature_der)
    half = order // 2
    if s > half:
        s = order - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def sign_image(key, image: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    # Order of NIST P-256.
    order = int(
        "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
    )
    der = key.sign(image, ec.ECDSA(hashes.SHA256()))
    return _low_s_rs(der, order)


def compressed_pubkey_hex(key) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pub = key.public_key()
    # Compressed SEC1 is 33 bytes.
    raw = pub.public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
    return raw.hex()


def verify_image(pubkey_bytes: bytes, image: bytes, sig_rs: bytes) -> bool:
    """Verify a 64-byte r‖s sig against a compressed SEC1 pubkey (33 bytes)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pubkey_bytes)
    except ValueError:
        return False
    if len(sig_rs) != 64:
        return False
    r = int.from_bytes(sig_rs[:32], "big")
    s = int.from_bytes(sig_rs[32:], "big")
    der = encode_dss_signature(r, s)
    try:
        pub.verify(der, image, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sign Orchard firmware release images (P-256).")
    p.add_argument("bins", nargs="*", type=Path, help=".bin files to sign")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--key-env", help="env var holding PEM private key contents")
    src.add_argument("--key-file", type=Path, help="path to PEM private key (local only)")
    p.add_argument("--sums", type=Path, default=None,
                   help="append .sig digests to this SHA256SUMS.txt")
    p.add_argument("--require", action="store_true",
                   help="fail if key is missing (production releases)")
    p.add_argument("--print-pubkey", action="store_true",
                   help="print compressed SEC1 pubkey hex and exit (needs key)")
    args = p.parse_args(argv)

    pem: bytes | None = None
    if args.key_env:
        raw = os.environ.get(args.key_env, "")
        if raw.strip():
            pem = raw.encode("utf-8") if isinstance(raw, str) else raw
    elif args.key_file:
        if args.key_file.is_file():
            pem = args.key_file.read_bytes()

    if pem is None:
        msg = (
            "UNSIGNED: no OTA signing key available "
            f"({args.key_env or args.key_file or 'no --key-*'}). "
            "Releases stay unsigned until OWNER adds OTA_SIGNING_KEY "
            "(see docs/security/SIGNED_OTA.md)."
        )
        print(msg, file=sys.stderr)
        if args.require or args.print_pubkey:
            return 2
        return 0

    try:
        key = _load_signing_key(pem)
    except Exception as e:  # noqa: BLE001 — surface parse errors clearly
        print(f"error: failed to load signing key: {e}", file=sys.stderr)
        return 2

    if args.print_pubkey:
        print(compressed_pubkey_hex(key))
        return 0

    if not args.bins:
        print("error: no .bin files given", file=sys.stderr)
        return 2

    signed = 0
    for bin_path in args.bins:
        if not bin_path.is_file():
            print(f"error: not a file: {bin_path}", file=sys.stderr)
            return 2
        image = bin_path.read_bytes()
        sig = sign_image(key, image)
        sig_path = bin_path.with_suffix(bin_path.suffix + ".sig")
        # Prefer foo.bin.sig over foo.sig for clarity.
        if bin_path.suffix == ".bin":
            sig_path = Path(str(bin_path) + ".sig")
        sig_path.write_bytes(sig)
        digest = hashlib.sha256(sig).hexdigest()
        print(f"SIGNED {bin_path.name} -> {sig_path.name} "
              f"(sig_sha256={digest[:16]}…)")
        signed += 1
        if args.sums is not None:
            line = f"{hashlib.sha256(sig).hexdigest()}  {sig_path.name}\n"
            with args.sums.open("a", encoding="utf-8") as f:
                f.write(line)

    print(f"signed {signed} image(s); pubkey={compressed_pubkey_hex(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
