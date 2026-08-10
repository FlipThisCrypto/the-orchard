# SPDX-License-Identifier: Apache-2.0
"""Configuration for the Season attestation writer.

Loads from ``chia/config.yaml`` (operator-private; gitignored) and
falls back to defaults when fields are missing. Operators copy
``chia/config.example.yaml`` to ``chia/config.yaml`` and fill in
local node + DataLayer paths.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
SIGNING_KEY_PATH = Path(__file__).resolve().parents[1] / "data" / "oracle_signing_key.hex"


@dataclass
class FullNodeConfig:
    host: str
    port: int
    cert_path: str
    key_path: str


@dataclass
class DataLayerConfig:
    host: str
    port: int
    cert_path: str
    key_path: str
    store_id: str
    # On-chain transaction fee for batch_update writes, in mojos (1 XCH =
    # 1e12 mojos). 0 = node default; raise it if writes stall unconfirmed
    # under mempool congestion. See datalayer/rpc.py::batch_update.
    fee: int = 0


@dataclass
class OracleConfig:
    url: str = "http://127.0.0.1:8000"
    # Shared secret proving this process is the operator's own writer/payout job
    # (same token as the oracle's ORCHARD_ORACLE_WRITER_TOKEN). Needed to read
    # operator-private fields such as wallet_address when the job does NOT run
    # on the oracle host. Empty = rely on loopback trust.
    writer_token: str = ""


@dataclass
class AttestationConfig:
    # How many Seasons before the current one to attest.
    # Default: all closed Seasons (None = no limit).
    max_lookback_seasons: int | None = None
    # Skip Seasons where uptime_hours is 0 (Tree never reported during them).
    skip_empty_seasons: bool = True


@dataclass
class Config:
    network: str
    full_node: FullNodeConfig
    data_layer: DataLayerConfig
    oracle: OracleConfig
    attestation: AttestationConfig
    signing_key_hex: str


def _expand(path_str: str) -> str:
    return os.path.expandvars(os.path.expanduser(path_str))


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"chia/config.yaml not found at {CONFIG_PATH}. "
            f"Copy chia/config.example.yaml to chia/config.yaml and edit."
        )
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    fn = raw.get("full_node", {})
    dl = raw.get("datalayer", {}) or raw.get("data_layer", {})
    orcl = raw.get("oracle", {})
    att = raw.get("attestation", {}) or {}

    return Config(
        network=raw.get("network", "mainnet"),
        full_node=FullNodeConfig(
            host=fn.get("host", "127.0.0.1"),
            port=int(fn.get("port", 8555)),
            cert_path=_expand(fn.get("cert_path", "")),
            key_path=_expand(fn.get("key_path", "")),
        ),
        data_layer=DataLayerConfig(
            host=dl.get("host", "127.0.0.1"),
            port=int(dl.get("port", 8562)),
            cert_path=_expand(dl.get("cert_path", "")),
            key_path=_expand(dl.get("key_path", "")),
            store_id=dl.get("store_id", ""),
            fee=int(dl.get("fee", 0) or 0),
        ),
        oracle=OracleConfig(
            url=orcl.get("url", "http://127.0.0.1:8000"),
            # Env override so the secret can stay out of config.yaml.
            writer_token=(
                os.environ.get("ORCHARD_ORACLE_WRITER_TOKEN")
                or orcl.get("writer_token", "")
                or ""
            ),
        ),
        attestation=AttestationConfig(
            max_lookback_seasons=att.get("max_lookback_seasons"),
            skip_empty_seasons=bool(att.get("skip_empty_seasons", True)),
        ),
        signing_key_hex=_load_or_make_signing_key(),
    )


def _restrict_signing_key_perms() -> None:
    """Force the signing-key file to 0600 (owner read/write only).

    Phase 5 attestations are signed with this key. If it leaks, an
    attacker can forge attestations the payout script accepts. Default
    umask leaves it group/other-readable on many Unix systems; tighten
    on every load so the chmod survives operator edits and file
    re-creations.

    On Windows ``os.chmod`` only flips the read-only bit (NTFS ACLs
    aren't POSIX modes). Real protection there comes from the parent
    directory's ACL, which is why we put the key under the project
    tree owned by the operator's user account.
    """
    try:
        os.chmod(SIGNING_KEY_PATH, 0o600)
    except OSError as e:
        print(f"[orchard.datalayer] WARN: could not chmod "
              f"{SIGNING_KEY_PATH} to 0600: {e}", file=sys.stderr)


# Written the first time a key is minted, and never deleted by code. Its
# presence means "a season key has existed on this machine", which is the fact
# the minting guard needs and the one a wiped key file destroys.
KEY_SENTINEL_PATH = SIGNING_KEY_PATH.with_suffix(".existed")


class SigningKeyError(RuntimeError):
    """The season signing key is missing when history says it should exist."""


def _load_or_make_signing_key() -> str:
    """Per-oracle signing key. Minted on GENUINE first run only.

    It used to regenerate whenever the file was missing. That is silent key
    rotation: a wiped data dir, a moved checkout, or a bad restore produced a
    fresh key, every new attest verified against a signer with no relationship
    to the store's history, and the next publish would have rewritten
    meta:schema to bless the new pubkey. From the outside, indistinguishable
    from key theft — an adversarial review rated it fatal.

    A sentinel file records that a key has ever existed here. Key present:
    load it. Key absent but sentinel present: REFUSE, loudly — restoring the
    key from backup is the fix, and deliberate rotation must be a visible act
    (remove the sentinel by hand after superseding the on-chain meta), not a
    side effect of a missing file. Both absent: genuine first run, mint.

    The file is forced to mode 0600 (owner-only) on every load — see
    ``_restrict_signing_key_perms`` for the Windows caveat.
    """
    SIGNING_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SIGNING_KEY_PATH.exists():
        text = SIGNING_KEY_PATH.read_text(encoding="utf-8").strip()
        if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
            _restrict_signing_key_perms()
            if not KEY_SENTINEL_PATH.exists():
                KEY_SENTINEL_PATH.write_text(
                    "a season signing key has existed on this machine; "
                    "its absence is now an error, not a first run\n",
                    encoding="utf-8")
            return text.upper()
        raise SigningKeyError(
            f"{SIGNING_KEY_PATH} exists but does not contain a 64-hex key. "
            f"Refusing to overwrite it with a fresh one — a corrupted key "
            f"file is evidence of a problem, and regenerating would sign new "
            f"records with a key unrelated to everything already on chain.")
    if KEY_SENTINEL_PATH.exists():
        raise SigningKeyError(
            f"the season signing key at {SIGNING_KEY_PATH} is MISSING, but "
            f"{KEY_SENTINEL_PATH.name} records that one has existed here. "
            f"Refusing to mint a replacement: every record already on chain "
            f"was signed by the old key, and silently rotating is "
            f"indistinguishable from key theft to anyone verifying. Restore "
            f"the key file from backup. If rotation is genuinely intended, "
            f"remove the sentinel by hand after superseding the on-chain "
            f"meta:schema record.")
    # Generate fresh. Write via os.open with O_CREAT|O_WRONLY and an
    # explicit 0o600 mode so the file is born owner-only on POSIX —
    # closes the small race where a previous default-umask write could
    # leak the bytes to a concurrent reader before chmod lands.
    import secrets
    new_hex = secrets.token_hex(32).upper()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(SIGNING_KEY_PATH), flags, 0o600)
    try:
        os.write(fd, (new_hex + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_signing_key_perms()  # belt-and-braces on Windows
    KEY_SENTINEL_PATH.write_text(
        "a season signing key has existed on this machine; its absence is "
        "now an error, not a first run\n", encoding="utf-8")
    return new_hex
