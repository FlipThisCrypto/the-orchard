# SPDX-License-Identifier: Apache-2.0
"""Reference implementation of the Orchard DataLayer publish schema (v1.0).

Implements ``docs/datalayer/SPEC.md`` — the five-namespace store:

    meta:schema
    node:<NODE_ID>
    readings:<NODE_ID>:<SEASON>:<HOUR>
    attest:<NODE_ID>:<SEASON>
    latest:<NODE_ID>

Pure functions plus a thin secp256r1/ECDSA wrapper (ADR-0007: ``ecdsa`` for
RFC 6979 deterministic signing, ``cryptography`` for verification). No I/O, no
network. Canonicalization matches ``orchard_chia.datalayer.attest`` byte-for-
byte (``sort_keys=True, separators=(",", ":")``, UTF-8), so the existing
HMAC-era code and this module agree on bytes.

This is the cross-language contract: the firmware (C++) and the public verifier
(JS/Python) must reproduce the canonical bytes, signatures, leaf hashes, and
roots produced here. The committed ``testdata/vectors.json`` is the golden
fixture both sides test against.
"""
from __future__ import annotations

import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_string

from . import merkle

SCHEMA_VERSION = "1.0.0"
HOURS_PER_SEASON = 24  # v1 day-aligned Season; block-aligned generalizes later.

# Integer fixed-point metric keys → how the dashboard renders them.
# human_value = raw * 10**pow10. Everything signed is an integer; the scale
# lives here (unsigned config), so floats never enter a signed payload.
DEFAULT_UNITS = {
    "temperature_mc":     {"display": "°C",    "pow10": -3},
    "humidity_milli_pct": {"display": "%RH",   "pow10": -3},
    "pressure_pa":        {"display": "hPa",   "pow10": -2},
    "gas_adc_raw":        {"display": "adc",   "pow10":  0},
    "gas_mv":             {"display": "mV",    "pow10":  0},
    "pm25_ugm3_x100":     {"display": "µg/m³", "pow10": -2},
    "pm10_ugm3_x100":     {"display": "µg/m³", "pow10": -2},
    "gps_sats":           {"display": "sats",  "pow10":  0},
}


# --------------------------------------------------------------------------- #
# Canonicalization & key/value encoding                                       #
# --------------------------------------------------------------------------- #
def canonical_bytes(obj: dict) -> bytes:
    """The one canonicalization rule, shared by every signer and hasher."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _key(ascii_key: str) -> str:
    """Hex-encoded UTF-8 of a colon-delimited ASCII key (DataLayer wants hex)."""
    return ascii_key.encode("utf-8").hex()


def meta_key() -> str:
    return _key("meta:schema")


def node_key(node_id: str) -> str:
    return _key(f"node:{node_id.upper()}")


def readings_key(node_id: str, season: int, hour: int) -> str:
    return _key(f"readings:{node_id.upper()}:{int(season):08d}:{int(hour):02d}")


def attest_key(node_id: str, season: int) -> str:
    return _key(f"attest:{node_id.upper()}:{int(season):08d}")


def latest_key(node_id: str) -> str:
    return _key(f"latest:{node_id.upper()}")


def value_hex(obj: dict) -> str:
    """Hex-encoded UTF-8 canonical JSON — what gets handed to batch_update."""
    return canonical_bytes(obj).hex()


def parse_value(value_hex_str: str | None) -> dict | None:
    """Inverse of :func:`value_hex`. Returns None on any decode failure."""
    if not value_hex_str:
        return None
    try:
        return json.loads(bytes.fromhex(value_hex_str).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# secp256r1 (P-256) — device & oracle signatures (ADR-0007)                   #
# --------------------------------------------------------------------------- #
# Encodings (SPEC §4): pubkey = 33-byte compressed SEC1, lowercase hex.
# Signature = fixed 64-byte r||s (two 32-byte big-endian ints), low-S
# normalized, lowercase hex. Signed digest = sha256(canonical bytes).
#
# Signing is RFC 6979 *deterministic* ECDSA: re-signing a body always
# reproduces the committed vector, and the firmware (mbedTLS deterministic
# ECDSA, same RFC) emits byte-identical signatures for identical inputs —
# that is the cross-language contract the golden vectors pin. CLVM's
# `secp256r1_verify` consumes exactly (pubkey33, digest32, sig64), which is
# what makes Tree signatures on-chain verifiable (ADR-0008).

_P256_N = NIST256p.order  # group order n, for low-S normalization


def generate_seed() -> str:
    """Fresh secp256r1 private scalar (32 bytes big-endian), hex. The device
    does this in NVS on first boot; the oracle does it once for the Season
    signer. (Named "seed" for continuity; for ECDSA it IS the private key.)"""
    sk = ec.generate_private_key(ec.SECP256R1())
    return sk.private_numbers().private_value.to_bytes(32, "big").hex()


def pubkey_for_seed(seed_hex: str) -> str:
    """Compressed SEC1 public key (33 bytes, hex) for a private scalar."""
    sk = ec.derive_private_key(int(seed_hex, 16), ec.SECP256R1())
    return sk.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    ).hex()


def reject_floats(obj: object, path: str = "$") -> None:
    """Raise if a to-be-signed payload contains any float.

    Enforces SPEC §0: a signed payload must be byte-stable across Python, C++,
    JS, and future languages, and floats are not (``1.00`` vs ``1.0`` vs
    ``1.0000001``). Sensor values must be integer fixed-point. ``bool`` is fine
    (JSON ``true``/``false`` is stable and is not a ``float``).
    """
    if isinstance(obj, float):
        raise ValueError(
            f"signed payload contains a float at {path}: {obj!r} — "
            f"use integer fixed-point (SPEC §0 / units table)"
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            reject_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            reject_floats(v, f"{path}[{i}]")


def _sign(body: dict, seed_hex: str) -> str:
    reject_floats(body)  # determinism guard — never sign a float
    digest = hashlib.sha256(canonical_bytes(body)).digest()
    sk = SigningKey.from_secret_exponent(int(seed_hex, 16), curve=NIST256p)
    raw = sk.sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=sigencode_string
    )
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if s > _P256_N // 2:  # low-S: every verifier accepts it, strict ones require it
        s = _P256_N - s
    return (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()


def _verify(body: dict, sig_hex: str, pubkey_hex: str) -> bool:
    # Robust to hostile/corrupt on-chain input: a node: card with a null or
    # non-string pubkey (or a non-string sig) must yield False, never an
    # exception — verify_bundle is contracted never to raise on bad data.
    if not isinstance(sig_hex, str) or not isinstance(pubkey_hex, str):
        return False
    try:
        sig = bytes.fromhex(sig_hex)
        if len(sig) != 64:
            return False
        pk = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes.fromhex(pubkey_hex)
        )
        der = encode_dss_signature(
            int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        )
        pk.verify(der, canonical_bytes(body), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign_reading(reading_without_sig: dict, seed_hex: str) -> dict:
    """Return the reading plus a secp256r1 ``sig`` over its canonical bytes."""
    return {**reading_without_sig, "sig": _sign(reading_without_sig, seed_hex)}


def verify_reading(reading_with_sig: dict, pubkey_hex: str) -> bool:
    """Check a reading's ``sig`` against the node's published pubkey."""
    sig = reading_with_sig.get("sig")
    if not sig:
        return False
    body = {k: v for k, v in reading_with_sig.items() if k != "sig"}
    return _verify(body, sig, pubkey_hex)


def sign_attest(attest_payload: dict, seed_hex: str) -> dict:
    """Add the oracle's secp256r1 ``oracle_sig`` over the attest payload."""
    body = {k: v for k, v in attest_payload.items() if k != "oracle_sig"}
    return {**attest_payload, "oracle_sig": _sign(body, seed_hex)}


def verify_attest(signed_attest: dict, pubkey_hex: str) -> bool:
    """Check an attest record's ``oracle_sig`` against the oracle pubkey."""
    sig = signed_attest.get("oracle_sig")
    if not sig:
        return False
    body = {k: v for k, v in signed_attest.items() if k != "oracle_sig"}
    return _verify(body, sig, pubkey_hex)


# --------------------------------------------------------------------------- #
# Merkle roots over readings                                                   #
# --------------------------------------------------------------------------- #
def _sorted_readings(readings: list[dict]) -> list[dict]:
    """Deterministic order: (ts_ms, sig) ascending. Independent of arrival."""
    return sorted(readings, key=lambda r: (r.get("ts_ms", 0), r.get("sig", "")))


def reading_leaf(reading_with_sig: dict) -> bytes:
    """Merkle leaf for a signed reading = leaf_hash(canonical(reading))."""
    return merkle.leaf_hash(canonical_bytes(reading_with_sig))


def hour_root(readings: list[dict]) -> str:
    """Merkle root (hex) over an hour's signed readings, sorted canonically."""
    leaves = [reading_leaf(r) for r in _sorted_readings(readings)]
    return merkle.merkle_root(leaves).hex()


def season_root(hour_roots_by_hour: dict[int, str]) -> str:
    """Merkle root (hex) over the present hour roots, ascending by hour.

    Hour roots are used as the season tree's leaves **directly** (not re-hashed),
    so a one-hour Season's root equals that hour's root and a reading→Season
    proof is the hour proof followed by the season proof (SPEC §5).
    """
    leaves = [bytes.fromhex(hour_roots_by_hour[h]) for h in sorted(hour_roots_by_hour)]
    return merkle.merkle_root(leaves).hex()


# --------------------------------------------------------------------------- #
# Uptime vs. Season score (the verifiable reward metric — SPEC §3)            #
# --------------------------------------------------------------------------- #
def verified_hours(readings_by_hour: dict[int, list[dict]], pubkey_hex: str) -> int:
    """Hours containing ≥1 signature-valid reading. Recomputable by anyone
    from the public ``readings:`` rows — this is what "verify the oracle" means.
    """
    return sum(
        1
        for readings in readings_by_hour.values()
        if any(verify_reading(r, pubkey_hex) for r in readings)
    )


def season_score(verified_hrs: int, hours_per_season: int = HOURS_PER_SEASON) -> int:
    """Public 0–100 reward metric. Integer round-half-up so every language
    agrees: ``(100*v + h//2) // h``. v1 = pure verified uptime; any future
    multiplier MUST stay recomputable from public data (SPEC §3)."""
    v = max(0, int(verified_hrs))
    return (100 * v + hours_per_season // 2) // hours_per_season


# --------------------------------------------------------------------------- #
# Record builders (one per namespace)                                         #
# --------------------------------------------------------------------------- #
def build_meta(
    *,
    writer_version: str,
    created_at: str,
    season_pubkey: str | None = None,
    geohash_precision: int = 5,
    operator_pass_nft: str | None = None,
    units: dict | None = None,
    season_sig: str = "secp256r1",
) -> dict:
    return {
        "orchard_schema": SCHEMA_VERSION,
        "store_role": "orchard-operator",
        "operator_pass_nft": operator_pass_nft,
        "units": units if units is not None else dict(DEFAULT_UNITS),
        "geohash_precision": int(geohash_precision),
        # season_pubkey is how anyone verifies the oracle's season signature —
        # it must be published, not just the scheme name (the verifier reads it
        # from here in both offline and live mode).
        "signer": {
            "device_sig": "secp256r1",
            "season_sig": season_sig,
            "season_pubkey": season_pubkey,
        },
        "writer_version": writer_version,
        "created_at": created_at,
    }


def build_node(
    *,
    node_id: str,
    pubkey: str,
    board: str,
    fw: str,
    sensors: list[dict],
    geohash: str,
    first_seen_utc: str,
    label: str | None = None,
) -> dict:
    return {
        "node_id": node_id.upper(),
        "pubkey": pubkey,
        "board": board,
        "fw": fw,
        "sensors": sensors,
        "geohash": geohash,
        "first_seen_utc": first_seen_utc,
        "label": label,
    }


def build_readings_batch(
    *, node_id: str, season: int, hour: int, readings: list[dict]
) -> dict:
    """An hourly batch. Readings are stored in canonical (ts_ms, sig) order and
    ``hour_root`` commits to exactly that ordered set."""
    ordered = _sorted_readings(readings)
    return {
        "node_id": node_id.upper(),
        "season": int(season),
        "hour": int(hour),
        "count": len(ordered),
        "readings": ordered,
        "hour_root": hour_root(ordered),
    }


def build_attest(
    *,
    node_id: str,
    season: int,
    season_start_utc: str,
    season_end_utc: str,
    hours_online: int,
    verified_hrs: int,
    reading_count: int,
    block_height_at_write: int,
    season_root_hex: str,
    signed_at: str,
) -> dict:
    """Unsigned attest payload. ``data_hash`` is kept (== ``season_root``) so the
    existing payout ``reader.py`` keeps working. Call :func:`sign_attest` next."""
    return {
        "node_id": node_id.upper(),
        "season": int(season),
        "season_start_utc": season_start_utc,
        "season_end_utc": season_end_utc,
        "hours_online": int(hours_online),
        "verified_hours": int(verified_hrs),
        "season_score": season_score(verified_hrs),
        "reading_count": int(reading_count),
        "block_height_at_write": int(block_height_at_write),
        "data_hash": season_root_hex,
        "season_root": season_root_hex,
        "signed_at": signed_at,
    }


def build_latest(
    *,
    node_id: str,
    season: int,
    hour: int,
    last_sealed_season: int | None,
    running_hours_online: int,
    last_reading: dict,
    updated_at: str,
) -> dict:
    return {
        "node_id": node_id.upper(),
        "season": int(season),
        "hour": int(hour),
        "last_sealed_season": last_sealed_season,
        "running_hours_online": int(running_hours_online),
        "last_reading": last_reading,
        "updated_at": updated_at,
    }
