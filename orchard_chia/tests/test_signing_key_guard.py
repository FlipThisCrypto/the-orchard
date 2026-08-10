# SPDX-License-Identifier: Apache-2.0
"""The season signing key never regenerates itself.

It used to: a missing key file minted a fresh key on the next load. A wiped
data dir, a moved checkout or a bad restore silently rotated the signer, every
new attest verified against a key with no relationship to the store's history,
and the next publish would have rewritten meta:schema to bless the new pubkey.
From the outside that is indistinguishable from key theft — the adversarial
review rated it fatal.

A sentinel records that a key has ever existed. Rotation is a visible human
act, never a side effect of a missing file.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import config as cfg_mod


@pytest.fixture()
def keydir(tmp_path, monkeypatch):
    key = tmp_path / "oracle_signing_key.hex"
    sentinel = tmp_path / "oracle_signing_key.existed"
    monkeypatch.setattr(cfg_mod, "SIGNING_KEY_PATH", key)
    monkeypatch.setattr(cfg_mod, "KEY_SENTINEL_PATH", sentinel)
    return key, sentinel


def test_a_genuine_first_run_mints_and_leaves_a_sentinel(keydir):
    key, sentinel = keydir
    hexkey = cfg_mod._load_or_make_signing_key()
    assert len(hexkey) == 64
    assert key.exists() and sentinel.exists()


def test_loading_an_existing_key_returns_it(keydir):
    key, _ = keydir
    key.write_text("AB" * 32 + "\n", encoding="utf-8")
    assert cfg_mod._load_or_make_signing_key() == "AB" * 32


def test_a_missing_key_with_history_refuses_to_mint(keydir):
    """The fatal case: the file vanished but a key has existed here."""
    key, sentinel = keydir
    sentinel.write_text("existed\n", encoding="utf-8")
    with pytest.raises(cfg_mod.SigningKeyError, match="indistinguishable from key theft"):
        cfg_mod._load_or_make_signing_key()
    assert not key.exists(), "and nothing may be minted in passing"


def test_the_refusal_names_the_recovery(keydir):
    _, sentinel = keydir
    sentinel.write_text("existed\n", encoding="utf-8")
    with pytest.raises(cfg_mod.SigningKeyError, match="Restore the key file from backup"):
        cfg_mod._load_or_make_signing_key()


def test_a_corrupted_key_file_is_not_overwritten(keydir):
    key, _ = keydir
    key.write_text("not hex at all\n", encoding="utf-8")
    with pytest.raises(cfg_mod.SigningKeyError, match="does not contain a 64-hex key"):
        cfg_mod._load_or_make_signing_key()
    assert key.read_text(encoding="utf-8") == "not hex at all\n", "evidence preserved"


def test_loading_a_pre_sentinel_key_backfills_the_sentinel(keydir):
    """Existing deployments get the protection on their next load."""
    key, sentinel = keydir
    key.write_text("CD" * 32 + "\n", encoding="utf-8")
    cfg_mod._load_or_make_signing_key()
    assert sentinel.exists()
