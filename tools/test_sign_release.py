# SPDX-License-Identifier: Apache-2.0
"""Tests for tools.sign_release (local test keys only — never production)."""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tools import sign_release


@pytest.fixture()
def p256_pem(tmp_path: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "test-ota-release.key"
    path.write_bytes(pem)
    return path


def test_sign_and_verify_round_trip(p256_pem: Path, tmp_path: Path):
    image = tmp_path / "orchard-test.bin"
    image.write_bytes(b"\x00firmware-bytes\xff" * 64)
    assert sign_release.main(["--key-file", str(p256_pem), str(image)]) == 0
    sig_path = Path(str(image) + ".sig")
    assert sig_path.is_file()
    sig = sig_path.read_bytes()
    assert len(sig) == 64

    key = serialization.load_pem_private_key(p256_pem.read_bytes(), password=None)
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )
    assert sign_release.verify_image(pub, image.read_bytes(), sig) is True
    # Tamper
    bad = bytearray(image.read_bytes())
    bad[0] ^= 0xFF
    assert sign_release.verify_image(pub, bytes(bad), sig) is False


def test_missing_key_is_soft_unsigned(tmp_path: Path, monkeypatch):
    image = tmp_path / "x.bin"
    image.write_bytes(b"abc")
    monkeypatch.delenv("OTA_SIGNING_KEY", raising=False)
    assert sign_release.main(["--key-env", "OTA_SIGNING_KEY", str(image)]) == 0
    assert not Path(str(image) + ".sig").exists()


def test_require_fails_without_key(tmp_path: Path, monkeypatch):
    image = tmp_path / "x.bin"
    image.write_bytes(b"abc")
    monkeypatch.delenv("OTA_SIGNING_KEY", raising=False)
    assert sign_release.main(
        ["--require", "--key-env", "OTA_SIGNING_KEY", str(image)]
    ) == 2


def test_print_pubkey(p256_pem: Path):
    # Capture via return path — main prints to stdout.
    assert sign_release.main(["--key-file", str(p256_pem), "--print-pubkey"]) == 0
