# SPDX-License-Identifier: Apache-2.0
"""DataLayer transaction-fee config plumbing."""
from __future__ import annotations

from orchard_chia.datalayer import config
from orchard_chia.datalayer.config import DataLayerConfig


def test_datalayer_config_fee_default_zero():
    c = DataLayerConfig("h", 8562, "c", "k", "store")
    assert c.fee == 0


def test_load_reads_datalayer_fee(tmp_path, monkeypatch):
    cfg_text = (
        "network: mainnet\n"
        "datalayer:\n"
        "  host: 127.0.0.1\n"
        "  port: 8562\n"
        "  store_id: abc123\n"
        "  fee: 100000000\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    # Avoid touching the real signing-key file on disk.
    monkeypatch.setattr(config, "_load_or_make_signing_key", lambda: "AB" * 32)

    c = config.load()
    assert c.data_layer.fee == 100_000_000
    assert c.data_layer.store_id == "abc123"


def test_load_fee_absent_defaults_zero(tmp_path, monkeypatch):
    cfg_text = (
        "network: mainnet\n"
        "datalayer:\n"
        "  store_id: abc123\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    monkeypatch.setattr(config, "_load_or_make_signing_key", lambda: "AB" * 32)

    c = config.load()
    assert c.data_layer.fee == 0
