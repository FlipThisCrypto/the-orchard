# SPDX-License-Identifier: Apache-2.0
"""DataLayer preflight checks."""
from __future__ import annotations

from orchard_chia.datalayer import preflight
from orchard_chia.datalayer.oracle import OracleError


def test_preflight_missing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchard_chia.datalayer.preflight.cfg_mod.CONFIG_PATH",
        tmp_path / "nope.yaml",
    )
    rep = preflight.run_preflight(skip_chia=True)
    assert not rep.ok
    assert any(c.name == "config.yaml" and not c.ok for c in rep.checks)


def test_preflight_with_mock_config(tmp_path, monkeypatch):
    from orchard_chia.datalayer import config as cfg_mod
    from orchard_chia.datalayer.config import (
        AttestationConfig,
        Config,
        DataLayerConfig,
        FullNodeConfig,
        OracleConfig,
    )

    key = tmp_path / "key.hex"
    key.write_text("ab" * 32 + "\n", encoding="utf-8")
    cert = tmp_path / "c.crt"
    cert.write_text("x", encoding="utf-8")
    kfile = tmp_path / "c.key"
    kfile.write_text("y", encoding="utf-8")

    fake = Config(
        network="test",
        full_node=FullNodeConfig("127.0.0.1", 8555, str(cert), str(kfile)),
        data_layer=DataLayerConfig(
            "127.0.0.1", 8562, str(cert), str(kfile), "aa" * 32
        ),
        oracle=OracleConfig("http://127.0.0.1:9"),
        attestation=AttestationConfig(),
        signing_key_hex="ab" * 32,
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake)
    monkeypatch.setattr(cfg_mod, "SIGNING_KEY_PATH", key)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.yaml")

    class FakeOracle:
        def __init__(self, url, writer_token=None):
            pass

        def current_season(self):
            return 3

        def list_nodes(self):
            return [{"node_id": "AA" * 16}]

    monkeypatch.setattr(preflight, "OracleClient", FakeOracle)
    rep = preflight.run_preflight(skip_chia=True)
    assert any(c.name == "oracle" and c.ok for c in rep.checks)
    assert any(c.name == "datalayer.store_id" and c.ok for c in rep.checks)


def test_store_id_wellformed_helper():
    assert preflight.store_id_wellformed("ab" * 32) is True
    assert preflight.store_id_wellformed("0x" + "ab" * 32) is True
    assert preflight.store_id_wellformed("ab" * 31) is False   # too short
    assert preflight.store_id_wellformed("zz" * 32) is False   # non-hex
    assert preflight.store_id_wellformed("") is False
    assert preflight.store_id_wellformed(None) is False


def test_preflight_flags_malformed_store_id(tmp_path, monkeypatch):
    from orchard_chia.datalayer import config as cfg_mod
    from orchard_chia.datalayer.config import (
        AttestationConfig,
        Config,
        DataLayerConfig,
        FullNodeConfig,
        OracleConfig,
    )

    key = tmp_path / "key.hex"
    key.write_text("ab" * 32 + "\n", encoding="utf-8")
    cert = tmp_path / "c.crt"
    cert.write_text("x", encoding="utf-8")
    kfile = tmp_path / "c.key"
    kfile.write_text("y", encoding="utf-8")
    fake = Config(
        network="test",
        full_node=FullNodeConfig("127.0.0.1", 8555, str(cert), str(kfile)),
        data_layer=DataLayerConfig(
            "127.0.0.1", 8562, str(cert), str(kfile), "not-a-real-store-id"
        ),
        oracle=OracleConfig("http://127.0.0.1:9"),
        attestation=AttestationConfig(),
        signing_key_hex="ab" * 32,
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake)
    monkeypatch.setattr(cfg_mod, "SIGNING_KEY_PATH", key)

    class FakeOracle:
        def __init__(self, url, writer_token=None):
            pass

        def current_season(self):
            return 1

        def list_nodes(self):
            return []

    monkeypatch.setattr(preflight, "OracleClient", FakeOracle)
    rep = preflight.run_preflight(skip_chia=True)
    assert any(
        c.name == "datalayer.store_id" and not c.ok and "malformed" in c.detail
        for c in rep.checks
    )


def test_preflight_oracle_down(tmp_path, monkeypatch):
    from orchard_chia.datalayer import config as cfg_mod
    from orchard_chia.datalayer.config import (
        AttestationConfig,
        Config,
        DataLayerConfig,
        FullNodeConfig,
        OracleConfig,
    )

    key = tmp_path / "key.hex"
    key.write_text("cd" * 32 + "\n", encoding="utf-8")
    cert = tmp_path / "c.crt"
    cert.write_text("x", encoding="utf-8")
    kfile = tmp_path / "c.key"
    kfile.write_text("y", encoding="utf-8")
    fake = Config(
        network="test",
        full_node=FullNodeConfig("127.0.0.1", 8555, str(cert), str(kfile)),
        data_layer=DataLayerConfig(
            "127.0.0.1", 8562, str(cert), str(kfile), "bb" * 32
        ),
        oracle=OracleConfig("http://127.0.0.1:9"),
        attestation=AttestationConfig(),
        signing_key_hex="cd" * 32,
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake)
    monkeypatch.setattr(cfg_mod, "SIGNING_KEY_PATH", key)

    class BadOracle:
        def __init__(self, url, writer_token=None):
            pass

        def current_season(self):
            raise OracleError("down")

        def list_nodes(self):
            return []

    monkeypatch.setattr(preflight, "OracleClient", BadOracle)
    rep = preflight.run_preflight(skip_chia=True)
    assert any(c.name == "oracle" and not c.ok for c in rep.checks)
