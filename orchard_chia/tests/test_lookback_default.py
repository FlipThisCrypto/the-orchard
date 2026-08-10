# SPDX-License-Identifier: Apache-2.0
"""Attest lookback is bounded by default, and an explicit null is honoured.

Unbounded lookback re-read every season since each Tree's registration on
every run — ~150 RPCs per Tree per day at 75 seasons, growing forever, to
conclude "unchanged" each time. The bound is a default, not a wall: an
operator who writes `max_lookback_seasons: null` has stated a choice and
keeps unlimited; only the ABSENT key gets 45.
"""
from __future__ import annotations

from orchard_chia.datalayer import config as cfg_mod


def _load_with(tmp_path, monkeypatch, att_yaml: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
network: mainnet
oracle:
  url: "https://oracle.test"
datalayer:
  store_id: "{'ab' * 32}"
  host: "127.0.0.1"
  port: 8562
  cert_path: "c.pem"
  key_path: "k.pem"
{att_yaml}
""", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(cfg_mod, "SIGNING_KEY_PATH", tmp_path / "key.hex")
    monkeypatch.setattr(cfg_mod, "KEY_SENTINEL_PATH", tmp_path / "key.existed")
    return cfg_mod.load()


def test_an_absent_key_gets_the_bound(tmp_path, monkeypatch):
    c = _load_with(tmp_path, monkeypatch, "attestation: {}")
    assert c.attestation.max_lookback_seasons == 45


def test_an_explicit_null_stays_unlimited(tmp_path, monkeypatch):
    c = _load_with(tmp_path, monkeypatch,
                   "attestation:\n  max_lookback_seasons: null")
    assert c.attestation.max_lookback_seasons is None


def test_an_explicit_number_is_kept(tmp_path, monkeypatch):
    c = _load_with(tmp_path, monkeypatch,
                   "attestation:\n  max_lookback_seasons: 10")
    assert c.attestation.max_lookback_seasons == 10
