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

# 1.1.0 adds the attest verification-basis fields (seal_source / sigs_verified /
# root_mismatches) so a sealed record self-describes whether its verified_hours
# and season_root are proof-backed. Minor bump: same major, so verifiers built
# for 1.x keep working and pre-1.1 records still verify.
SCHEMA_VERSION = "1.1.0"
HOURS_PER_SEASON = 24  # v1 day-aligned Season; block-aligned generalizes later.

# Verification basis of a sealed attest (SPEC §2.4).
SEAL_SOURCE_READINGS = "readings"      # season_root is a real Merkle root
SEAL_SOURCE_PLACEHOLDER = "placeholder"  # no public readings; NOT proof-backed

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


def sign_digest(digest: bytes, seed_hex: str) -> str:
    """secp256r1-sign a raw 32-byte ``digest`` (RFC 6979 deterministic, low-S
    normalized); returns the 64-byte ``r||s`` signature as hex — the exact form
    CLVM's ``secp256r1_verify`` consumes (SPEC §4.1).

    Readings sign ``sha256(canonical_bytes(body))`` (see :func:`_sign`); on-chain
    heartbeats (ADR-0008 / HANDOVER T20) sign a digest the puzzle itself
    computes, so this lower-level signer is shared by both paths.
    """
    sk = SigningKey.from_secret_exponent(int(seed_hex, 16), curve=NIST256p)
    raw = sk.sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=sigencode_string
    )
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if s > _P256_N // 2:  # low-S: every verifier accepts it, strict ones require it
        s = _P256_N - s
    return (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()


def _sign(body: dict, seed_hex: str) -> str:
    reject_floats(body)  # determinism guard — never sign a float
    return sign_digest(hashlib.sha256(canonical_bytes(body)).digest(), seed_hex)


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
# How many signature-valid readings an hour must hold before it counts as an
# hour of sensing.
#
# Firmware samples every 60 s (firmware/include/config.h:13), so a healthy hour
# holds about 60 readings. The original rule was "≥1", which meant a Tree that
# reported once an hour earned exactly what a Tree reporting continuously
# earned — a 60x overstatement of the quantity the number names, and one that
# `payout/calculator.py` converts straight into tokens. Every signature was
# genuine; the claim built on them was not.
#
# 30 is half the expected cadence: high enough that an hour of near-silence
# cannot be sold as an hour of sensing, low enough that a reboot, a Wi-Fi drop
# or a few minutes of clock skew does not cost an operator the whole hour.
MIN_VERIFIED_READINGS_PER_HOUR = 30

# What "≥1" meant. Kept as a named constant so records written under the old
# rule can be re-verified exactly as they were written, rather than being
# retroactively judged by a rule that did not exist when they were signed.
LEGACY_MIN_READINGS_PER_HOUR = 1


# Clock-skew grace at season edges. A reading stamped 23:59:58 by a device a
# few seconds fast must not be thrown out of its own season; a reading from a
# DIFFERENT day must never be let in. Five minutes is far above real NTP skew
# and far below the 24h that would matter.
SEASON_WINDOW_GRACE_MS = 5 * 60 * 1000


def window_ms_from_utc(start_utc: str, end_utc: str) -> tuple[int, int] | None:
    """(start_ms, end_ms) from the ISO bounds an attest record declares.

    Returns None when unparseable — callers treat that as "no window", which
    keeps pre-window records verifiable exactly as they were written.
    """
    from datetime import datetime, timezone
    try:
        lo = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if lo.tzinfo is None:
        lo = lo.replace(tzinfo=timezone.utc)
    if hi.tzinfo is None:
        hi = hi.replace(tzinfo=timezone.utc)
    return (int(lo.timestamp() * 1000) - SEASON_WINDOW_GRACE_MS,
            int(hi.timestamp() * 1000) + SEASON_WINDOW_GRACE_MS)


def _in_window(reading: dict, window_ms: tuple[int, int] | None) -> bool:
    if window_ms is None:
        return True
    ts = reading.get("ts_ms")
    if not isinstance(ts, int) or isinstance(ts, bool):
        return False    # a reading with no timestamp cannot prove WHEN it was
    return window_ms[0] <= ts < window_ms[1]


def verified_hours(readings_by_hour: dict[int, list[dict]], pubkey_hex: str,
                   *, min_readings: int = MIN_VERIFIED_READINGS_PER_HOUR,
                   window_ms: tuple[int, int] | None = None) -> int:
    """Hours holding at least ``min_readings`` signature-valid readings.

    Recomputable by anyone from the public ``readings:`` rows — that is what
    "verify the oracle" means, and it is why ``min_readings`` is written INTO
    the attest record rather than left as a constant in this file. A threshold a
    verifier has to guess is a threshold that silently changes when this
    module does, retroactively invalidating records that were honest when
    signed.
    """
    if min_readings < 1:
        raise ValueError(
            f"min_readings must be at least 1, got {min_readings}. Zero would "
            f"credit an hour that contains no readings at all.")
    # The window is the cross-season replay defense: a signed reading carries
    # no season field, and the hour a record files it under is chosen by the
    # WRITER — but ts_ms is inside the device signature. Readings replayed
    # from a season the Tree was dark for carry their original timestamps,
    # fall outside this season's declared bounds, and stop counting toward
    # the quorum. hour_root stays a commitment to the published batch as-is;
    # only what an hour is WORTH applies the window.
    return sum(
        1
        for readings in readings_by_hour.values()
        if sum(1 for r in readings
               if _in_window(r, window_ms) and verify_reading(r, pubkey_hex))
        >= min_readings
    )


def schema_declares_basis(schema_version: str | None) -> bool:
    """Does a store on ``schema_version`` owe a ``seal_source`` declaration?

    True for >= 1.1.0. Used to close the downgrade hole: in a store that
    declares 1.1.0, an attest with NO basis is not "legacy", it is a record
    dodging its own caveat.

    Fails **closed**: only a version that positively parses as pre-1.1 (e.g.
    "1.0.0") is exempt. A present-but-unparseable version ("1", "v1.1.0",
    "next") is treated as declaring a basis, so a malformed string can't be used
    to re-open the exemption. Only an absent version is "unknown".
    """
    if schema_version is None:
        return False
    raw = str(schema_version).strip()
    if not raw:
        return False
    parts = raw.split(".")
    if len(parts) < 2:
        return True  # can't prove it's pre-1.1 — fail closed
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return True  # unparseable — fail closed
    return (major, minor) >= (1, 1)


def attest_basis(
    attest_record: dict, *, store_schema: str | None = None
) -> tuple[bool | None, str]:
    """How much of this sealed attest is actually proven? ``(proof_backed, why)``.

    ``True``  — declares ``seal_source == "readings"`` **and** ``sigs_verified``:
                ``season_root`` is a Merkle root over published readings whose
                device signatures were actually checked.
    ``False`` — not proof-backed, for any of:
                * ``seal_source == "placeholder"`` (nothing was published),
                * ``sigs_verified is False`` (hours counted by reading
                  *presence* because no device pubkey was available — the count
                  is not signature-derived, whatever the root says),
                * an unrecognized ``seal_source`` (fail closed on the unknown),
                * no declaration at all in a store that declares >= 1.1.0
                  (a record cannot dodge the caveat by omitting it).
    ``None``  — undeclared in a pre-1.1.0 / unknown store: genuinely unknown.
                Never treat as proven.
    """
    rec = attest_record or {}
    src = rec.get("seal_source")
    if src is None:
        if schema_declares_basis(store_schema):
            return False, (
                f"store declares schema {store_schema} but the attest declares "
                f"no seal_source — a >=1.1.0 record must state its basis"
            )
        return None, "record predates schema 1.1.0 and declares no basis"
    src = str(src)
    if src == SEAL_SOURCE_PLACEHOLDER:
        return False, (
            "seal_source=placeholder — no readings were published, so "
            "verified_hours/season_root prove nothing"
        )
    if src != SEAL_SOURCE_READINGS:
        return False, f"unrecognized seal_source {src!r} — cannot treat as proven"
    # sigs_verified must be POSITIVELY true. Honoring only the literal False
    # would let an oracle assert "signatures verified" by simply omitting the
    # field (or sending null/0/"false") — the same omission hole seal_source
    # closes. Anything that is not exactly True is not a claim of verification.
    sv = rec.get("sigs_verified")
    if sv is not True:
        return False, (
            f"seal_source=readings but sigs_verified={sv!r} is not true — hours "
            f"were not established by verifying device signatures"
        )
    return True, f"seal_source={src} (Merkle-rooted, signatures verified)"


def attest_declares_placeholder(attest_record: dict) -> bool:
    """Does the record explicitly declare the placeholder basis?"""
    return str((attest_record or {}).get("seal_source")) == SEAL_SOURCE_PLACEHOLDER


def placeholder_inconsistency(attest_record: dict) -> str | None:
    """Why a placeholder-declared record contradicts itself, or None if consistent.

    A placeholder means *nothing was published*, so the writer sets
    ``verified_hours``/``season_score``/``reading_count`` to 0. A record that
    declares placeholder while claiming a non-zero verified count is
    self-contradictory: it wants the leniency of "unproven" **and** the credit of
    a verified number. That is a definitive defect, not a cannot-verify.
    """
    rec = attest_record or {}
    if not attest_declares_placeholder(rec):
        return None
    bad = []
    for field in ("verified_hours", "season_score", "reading_count"):
        val = rec.get(field)
        if val is not None and int(val) != 0:
            bad.append(f"{field}={val}")
    if bad:
        return (
            "declares seal_source=placeholder (nothing published) yet claims "
            + ", ".join(bad)
        )
    return None


def attest_is_proof_backed(
    attest_record: dict, *, store_schema: str | None = None
) -> bool | None:
    """Tri-state convenience wrapper over :func:`attest_basis`."""
    return attest_basis(attest_record, store_schema=store_schema)[0]


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
    seal_source: str = SEAL_SOURCE_READINGS,
    sigs_verified: bool = True,
    root_mismatches: int = 0,
    min_readings_per_hour: int = MIN_VERIFIED_READINGS_PER_HOUR,
) -> dict:
    """Unsigned attest payload. ``data_hash`` is kept (== ``season_root``) so the
    existing payout ``reader.py`` keeps working. Call :func:`sign_attest` next.

    **Verification basis (schema 1.1.0).** The record self-describes how much of
    it is actually proven, so no consumer can mistake an oracle self-report for
    evidence:

    - ``seal_source`` — ``"readings"`` when ``season_root`` is a real Merkle root
      over published device-signed readings, ``"placeholder"`` when nothing was
      published and the root is only a hash of ``node:season:hours``. A
      placeholder record is **not proof-backed**; its ``verified_hours`` must be
      0 because nothing was verified.
    - ``sigs_verified`` — False when hours were counted by reading *presence*
      because no device pubkey was available to check signatures against.
    - ``root_mismatches`` — hours whose stored ``hour_root`` disagreed with a
      recompute (>0 is a tampering/corruption red flag).

    These are inside the signed body deliberately: an intermediary must not be
    able to strip the caveat and leave the number looking proven.
    """
    return {
        "node_id": node_id.upper(),
        "season": int(season),
        "season_start_utc": season_start_utc,
        "season_end_utc": season_end_utc,
        "hours_online": int(hours_online),
        "verified_hours": int(verified_hrs),
        "seal_source": str(seal_source),
        "sigs_verified": bool(sigs_verified),
        "root_mismatches": int(root_mismatches),
        "season_score": season_score(verified_hrs),
        # The rule verified_hours was computed under, written INTO the
        # signed body. Without it a verifier recomputes with whatever this
        # module currently says, so tightening the threshold would
        # retroactively brand honest older records as overstatements.
        "min_readings_per_hour": int(min_readings_per_hour),
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
