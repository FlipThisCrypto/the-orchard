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
    assert len(rep.checks) == 10
    assert _failed_names(rep) == set()
    assert "Anti-backdate anchor present" in {c.name for c in rep.checks}
    assert "Records agree on node and season" in {c.name for c in rep.checks}
    assert "Schema and signer scheme supported" in {c.name for c in rep.checks}


def test_unsupported_device_scheme_fails():
    b = _bundle()
    b["meta"]["signer"]["device_sig"] = "ed25519"
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Schema and signer scheme supported" in _failed_names(rep)


def test_incompatible_schema_major_fails():
    b = _bundle()
    b["meta"]["orchard_schema"] = "2.0.0"
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Schema and signer scheme supported" in _failed_names(rep)


def test_stitched_bundle_wrong_attest_node_fails():
    b = _bundle()
    b["attest"]["node_id"] = "0" * 32  # attest for a different node
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Records agree on node and season" in _failed_names(rep)


def test_stitched_bundle_wrong_readings_season_fails():
    b = _bundle()
    b["readings_records"][0]["season"] = b["attest"]["season"] + 99
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Records agree on node and season" in _failed_names(rep)


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


def test_merkle_proof_covers_every_reading():
    b = _bundle()
    n = len(b["readings_records"][0]["readings"])
    assert n >= 2  # vectors carry several readings
    rep = verify.verify_bundle(**b)
    mk = next(c for c in rep.checks if c.name == "Reading Merkle proof verified")
    assert mk.ok is True
    # Detail reports each reading proven, not a single sampled leaf.
    assert f"{n} reading(s) each proven" in mk.detail


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


def test_null_node_pubkey_does_not_crash():
    # A node: card with a null pubkey must yield INVALID, not a TypeError crash
    # (verify_bundle is contracted never to raise on bad data).
    b = _bundle()
    b["node"]["pubkey"] = None
    rep = verify.verify_bundle(**b)  # must not raise
    assert rep.valid is False
    assert "Device signature verified" in _failed_names(rep)


def test_non_hex_node_pubkey_does_not_crash():
    b = _bundle()
    b["node"]["pubkey"] = "not-hex!!"
    rep = verify.verify_bundle(**b)
    assert rep.valid is False


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


def test_bundle_proof_pairs_covers_all_verified_keys():
    from orchard_chia.datalayer import schema

    b = _bundle()
    node_id = b["node"]["node_id"]
    season = int(b["attest"]["season"])
    pairs = cli._bundle_proof_pairs(b, node_id, season)

    # Every key the verdict trusts is present …
    assert schema.meta_key() in pairs
    assert schema.node_key(node_id) in pairs
    assert schema.attest_key(node_id, season) in pairs
    hour = int(b["readings_records"][0]["hour"])
    assert schema.readings_key(node_id, season, hour) in pairs

    # … bound to the exact canonical value hex of the record being verified.
    assert pairs[schema.node_key(node_id)] == schema.value_hex(b["node"])
    assert pairs[schema.attest_key(node_id, season)] == schema.value_hex(b["attest"])
    assert pairs[schema.meta_key()] == schema.value_hex(b["meta"])
    assert pairs[schema.readings_key(node_id, season, hour)] == schema.value_hex(
        b["readings_records"][0]
    )


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
