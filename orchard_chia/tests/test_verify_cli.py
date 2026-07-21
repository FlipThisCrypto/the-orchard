# SPDX-License-Identifier: Apache-2.0
"""Tests for the orchard-verify engine + CLI (offline / vectors mode).

Covers the acceptance criteria: the golden vectors verify VALID, and each kind
of tampering (metric, node_id, Merkle proof, season score, oracle signature)
fails loudly. Plus CLI exit codes (0 valid / 1 invalid / 2 cannot-verify).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from orchard_chia.cli import orchard_verify as cli
from orchard_chia.datalayer import verify

VPATH = Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"
VEC = json.loads(VPATH.read_text(encoding="utf-8"))


def _bundle() -> dict:
    rec = copy.deepcopy(VEC["records"])
    return {
        "meta": rec["meta"],
        "node": rec["node"],
        "attest": rec["attest"],
        "readings_records": [rec["readings"]],
    }


def _failed_names(rep) -> set[str]:
    return {c.name for c in rep.checks if not c.ok}


# --- engine: happy path ---------------------------------------------------- #
def test_vectors_bundle_is_valid():
    rep = verify.verify_bundle(**_bundle())
    assert rep.valid is True
    assert len(rep.checks) == 7
    assert _failed_names(rep) == set()


# --- engine: tampering must fail loudly (acceptance criteria 2–6) ----------- #
def test_changed_metric_fails():
    b = _bundle()
    b["readings_records"][0]["readings"][0]["metrics"]["temperature_mc"] = 99999
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Device signature verified" in _failed_names(rep)


def test_changed_node_id_fails():
    b = _bundle()
    b["readings_records"][0]["readings"][0]["node_id"] = "0" * 32
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Device signature verified" in _failed_names(rep)


def test_changed_merkle_proof_fails():
    b = _bundle()
    hr = b["readings_records"][0]["hour_root"]
    b["readings_records"][0]["hour_root"] = ("1" if hr[0] == "0" else "0") + hr[1:]
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Reading Merkle proof verified" in _failed_names(rep)


def test_changed_season_score_fails():
    b = _bundle()
    b["attest"]["season_score"] = b["attest"]["season_score"] + 1
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Season score recomputed" in _failed_names(rep)


def test_changed_oracle_signature_fails():
    b = _bundle()
    sig = b["attest"]["oracle_sig"]
    b["attest"]["oracle_sig"] = ("1" if sig[0] == "0" else "0") + sig[1:]
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Oracle season signature verified" in _failed_names(rep)


def test_missing_oracle_pubkey_fails_loudly():
    b = _bundle()
    b["meta"]["signer"]["season_pubkey"] = None
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Oracle season signature verified" in _failed_names(rep)


# --- CLI: exit codes + output --------------------------------------------- #
def test_cli_vectors_valid_exit_zero(capsys):
    rc = cli.main(["vectors", str(VPATH)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Result: VALID" in out
    assert "Device signature verified" in out
    assert "Oracle season signature verified" in out


def test_cli_vectors_tampered_exit_one(tmp_path, capsys):
    data = copy.deepcopy(VEC)
    data["records"]["attest"]["season_score"] += 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    rc = cli.main(["vectors", str(bad)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Result: INVALID" in out


def test_cli_missing_file_exit_two(capsys):
    assert cli.main(["vectors", "does-not-exist.json"]) == 2


def test_cli_live_without_config_or_rpc_exit_two(capsys, monkeypatch, tmp_path):
    """Live mode is wired: missing config / unreachable RPC → exit 2 (cannot)."""
    # Point config at a non-existent path so load() fails cleanly.
    monkeypatch.setattr(
        "orchard_chia.cli.orchard_verify.config.CONFIG_PATH",
        tmp_path / "no-such-config.yaml",
    )
    rc = cli.main(
        ["live", "--store-id", "S", "--node-id", "N" * 32, "--season", "42", "--hour", "13"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "error:" in err.lower() or "not found" in err.lower()
