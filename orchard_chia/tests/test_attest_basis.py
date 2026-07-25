# SPDX-License-Identifier: Apache-2.0
"""Sealed attestations declare their verification basis (schema 1.1.0).

The defect this pins: the writer's placeholder path used to sign the oracle's
own ``hours_online`` into ``verified_hours`` — presenting a self-report as the
verifiable metric in exactly the case where nothing was published on chain.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from orchard_chia.datalayer import schema, verify
from orchard_chia.payout import calculator

VPATH = Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"
VEC = json.loads(VPATH.read_text(encoding="utf-8"))
SEED = "01" + "00" * 31


def _bundle() -> dict:
    rec = copy.deepcopy(VEC["records"])
    return {
        "meta": rec["meta"],
        "node": rec["node"],
        "attest": rec["attest"],
        "readings_records": [rec["readings"]],
    }


def _failed(rep):
    return {c.name for c in rep.checks if not c.ok}


# --- the signed record self-describes ------------------------------------- #
def test_build_attest_declares_basis_by_default():
    a = schema.build_attest(
        node_id="AB" * 16, season=5,
        season_start_utc="2026-05-31T00:00:00Z", season_end_utc="2026-06-01T00:00:00Z",
        hours_online=24, verified_hrs=24, reading_count=10,
        block_height_at_write=1, season_root_hex="ab" * 32,
        signed_at="2026-06-01T00:05:00Z",
    )
    assert a["seal_source"] == schema.SEAL_SOURCE_READINGS
    assert a["sigs_verified"] is True
    assert a["root_mismatches"] == 0


def test_basis_fields_are_inside_the_signature():
    """An intermediary must not be able to strip the caveat and leave the
    number looking proven."""
    body = schema.build_attest(
        node_id="AB" * 16, season=5,
        season_start_utc="s", season_end_utc="e",
        hours_online=24, verified_hrs=0, reading_count=0,
        block_height_at_write=1, season_root_hex="ab" * 32, signed_at="t",
        seal_source=schema.SEAL_SOURCE_PLACEHOLDER, sigs_verified=False,
    )
    signed = schema.sign_attest(body, SEED)
    pub = schema.pubkey_for_seed(SEED)
    assert schema.verify_attest(signed, pub) is True
    # Strip the caveat -> signature no longer verifies.
    stripped = {k: v for k, v in signed.items() if k != "seal_source"}
    assert schema.verify_attest(stripped, pub) is False
    # Flip it to look proven -> signature no longer verifies.
    lied = {**signed, "seal_source": schema.SEAL_SOURCE_READINGS}
    assert schema.verify_attest(lied, pub) is False


def test_attest_is_proof_backed_tri_state():
    proven = {"seal_source": "readings", "sigs_verified": True}
    assert schema.attest_is_proof_backed(proven) is True
    assert schema.attest_is_proof_backed({"seal_source": "placeholder"}) is False
    assert schema.attest_is_proof_backed({}) is None  # pre-1.1.0 record


def test_sigs_verified_must_be_positively_true():
    """Omitting/nulling the field must NOT read as 'signatures were verified' —
    otherwise an oracle asserts verification by saying nothing."""
    for bad in ({}, {"sigs_verified": None}, {"sigs_verified": 0},
                {"sigs_verified": "false"}, {"sigs_verified": "true"},
                {"sigs_verified": False}):
        rec = {"seal_source": "readings", **bad}
        assert schema.attest_is_proof_backed(rec) is False, bad


def test_schema_declares_basis_fails_closed():
    # Positively pre-1.1 -> exempt.
    assert schema.schema_declares_basis("1.0.0") is False
    assert schema.schema_declares_basis("1.0") is False
    # >= 1.1 -> must declare.
    assert schema.schema_declares_basis("1.1.0") is True
    assert schema.schema_declares_basis("2.0.0") is True
    # Present but unparseable -> fail CLOSED (can't be used to dodge).
    for weird in ("1", "v1.1.0", "next", "1.x"):
        assert schema.schema_declares_basis(weird) is True, weird
    # Absent -> genuinely unknown.
    assert schema.schema_declares_basis(None) is False
    assert schema.schema_declares_basis("") is False


# --- the verifier reports it honestly ------------------------------------- #
def test_placeholder_attest_flagged_not_called_tampering():
    b = _bundle()
    b["attest"]["seal_source"] = schema.SEAL_SOURCE_PLACEHOLDER
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Attestation is proof-backed" in _failed(rep)


def test_declared_root_mismatch_fails_loudly():
    b = _bundle()
    b["attest"]["root_mismatches"] = 2
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "No hour_root mismatches declared" in _failed(rep)


def test_omitting_basis_in_a_11_store_cannot_dodge_the_flag():
    """Anti-downgrade: a record can't look 'legacy' to escape its own caveat
    when the store it lives in declares 1.1.0."""
    b = _bundle()
    del b["attest"]["seal_source"]
    assert b["meta"]["orchard_schema"] == "1.1.0"
    rep = verify.verify_bundle(**b)
    basis = next(c for c in rep.checks if c.name == "Attestation is proof-backed")
    assert basis.ok is False
    assert "must state its basis" in basis.detail


def test_genuinely_legacy_store_record_is_unknown_not_unproven():
    """A pre-1.1.0 store legitimately has no basis field — that is unknown,
    and must not be reported as a placeholder."""
    b = _bundle()
    del b["attest"]["seal_source"]
    b["meta"]["orchard_schema"] = "1.0.0"
    rep = verify.verify_bundle(**b)
    basis = next(c for c in rep.checks if c.name == "Attestation is proof-backed")
    assert basis.ok is True
    assert "predates schema 1.1.0" in basis.detail


def test_null_basis_is_treated_like_omitted():
    b = _bundle()
    b["attest"]["seal_source"] = None
    rep = verify.verify_bundle(**b)
    basis = next(c for c in rep.checks if c.name == "Attestation is proof-backed")
    assert basis.ok is False


def test_unrecognized_basis_fails_closed():
    b = _bundle()
    b["attest"]["seal_source"] = "some-future-1.2-value"
    rep = verify.verify_bundle(**b)
    basis = next(c for c in rep.checks if c.name == "Attestation is proof-backed")
    assert basis.ok is False
    assert "unrecognized" in basis.detail


def test_presence_counted_hours_are_not_proof_backed():
    """seal_source=readings but sigs_verified=false: the count came from reading
    PRESENCE, not from checking device signatures."""
    b = _bundle()
    b["attest"]["sigs_verified"] = False
    rep = verify.verify_bundle(**b)
    basis = next(c for c in rep.checks if c.name == "Attestation is proof-backed")
    assert basis.ok is False
    assert "sigs_verified" in basis.detail and "not true" in basis.detail


def test_placeholder_is_cannot_verify_not_fraud_END_TO_END(tmp_path, capsys):
    """The real CLI on a real placeholder bundle must exit 2, not 1.

    Pins the defect the adversarial review caught: cmd_vectors used to return
    `0 if valid else 1`, and the season-root checks still ran against a
    placeholder root — so an honestly-labelled record was reported as fraud.
    """
    from orchard_chia.cli import orchard_verify as cli

    from orchard_chia.datalayer.attest import data_hash_for_uptime

    data = copy.deepcopy(VEC)
    a = data["records"]["attest"]
    # The REAL shape the writer emits: a placeholder season has NO published
    # readings, verified/score/count are 0, and the root is the placeholder
    # hash. (An earlier version of this test kept the golden readings and so
    # never exercised the real case — caught by adversarial review.)
    a["seal_source"] = schema.SEAL_SOURCE_PLACEHOLDER
    a["verified_hours"] = 0
    a["season_score"] = 0
    a["reading_count"] = 0
    a["sigs_verified"] = False
    ph_root = data_hash_for_uptime(
        a["node_id"], int(a["season"]), int(a["hours_online"])
    )
    a["season_root"] = ph_root
    a["data_hash"] = ph_root
    a.pop("oracle_sig", None)
    data["records"]["attest"] = schema.sign_attest(a, VEC["oracle"]["seed"])
    # No readings published for that season.
    data["records"]["readings"] = {
        **data["records"]["readings"], "readings": [], "count": 0,
        "hour_root": schema.hour_root([]),
    }

    p = tmp_path / "placeholder_vectors.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    rc = cli.main(["vectors", str(p)])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "CANNOT-VERIFY" in out
    # And it must NOT be described as tampering.
    assert "season root mismatch" not in out


def test_declared_mismatch_is_still_definitive_invalid():
    from orchard_chia.cli import orchard_verify as cli
    from orchard_chia.datalayer.inclusion import InclusionReport

    tampered = verify.Report(node_id="N", season=1, checks=[
        verify.Check(cli.INCLUSION_CHECK_NAME, True, ""),
        verify.Check("No hour_root mismatches declared", False, "2 hours"),
    ])
    assert cli._live_exit_code(tampered, InclusionReport(ok=True, detail="ok")) == 1
    assert cli._offline_exit_code(tampered) == 1


# --- the money defects the adversarial review caught ---------------------- #
def test_unknown_basis_never_inflates_payment():
    """REGRESSION: a non-'readings' seal_source must NOT route payment to the
    larger unverified claim. Caught by the adversarial money lens."""
    record = {
        "hours_online": 18, "verified_hours": 12,
        "seal_source": "some-future-1.2-value", "reading_count": 120,
    }
    hours, basis = calculator.paid_hours(record)
    assert hours == 12, "must not pay the unverified claim of 18"
    assert basis == "verified_hours (basis unrecognized)"
    assert calculator.juice_mojos_for_attestation(record, daily_rate=1.0) == 500


def test_inconsistent_placeholder_is_unpayable():
    """A record declaring placeholder (nothing published) while claiming a
    verified number is self-contradictory. The verifier calls that a definitive
    defect, so the payer must not quietly pay it either."""
    record = {"hours_online": 18, "verified_hours": 12,
              "seal_source": "placeholder", "reading_count": 120}
    hours, basis = calculator.paid_hours(record)
    assert hours == 0
    assert basis == "unpayable (inconsistent attestation)"
    assert calculator.juice_mojos_for_attestation(record, daily_rate=1.0) == 0


def test_inconsistent_placeholder_is_definitive_invalid():
    """And the verifier must contradict it rather than skipping its checks."""
    b = _bundle()
    b["attest"]["seal_source"] = schema.SEAL_SOURCE_PLACEHOLDER
    b["attest"]["verified_hours"] = 24  # claims a verified number anyway
    rep = verify.verify_bundle(**b)
    assert rep.valid is False
    assert "Placeholder attestation is self-consistent" in _failed(rep)
    from orchard_chia.cli import orchard_verify as cli
    assert cli._offline_exit_code(rep) == 1  # definitive, not cannot-verify


def test_declared_mismatch_cannot_be_buried_by_changing_basis():
    """A declared tampering signal must not be suppressible by the party that
    declares it — previously a non-'readings' basis dropped the check entirely."""
    for src in ("placeholder", "some-future-1.2-value", "readings"):
        b = _bundle()
        b["attest"]["seal_source"] = src
        b["attest"]["root_mismatches"] = 9
        rep = verify.verify_bundle(**b)
        names = {c.name for c in rep.checks}
        assert "No hour_root mismatches declared" in names, src
        from orchard_chia.cli import orchard_verify as cli
        assert cli._offline_exit_code(rep) == 1, src


def test_placeholder_root_must_be_well_formed():
    """Declaring 'placeholder' must not smuggle an arbitrary root through
    unchecked — it must equal sha256(node:season:hours_online)."""
    b = _bundle()
    b["attest"]["seal_source"] = schema.SEAL_SOURCE_PLACEHOLDER
    b["attest"]["verified_hours"] = 0
    b["attest"]["season_score"] = 0
    b["attest"]["reading_count"] = 0
    b["attest"]["season_root"] = "ff" * 32
    b["attest"]["data_hash"] = "ff" * 32
    rep = verify.verify_bundle(**b)
    assert "Placeholder root well-formed" in _failed(rep)
    from orchard_chia.cli import orchard_verify as cli
    assert cli._offline_exit_code(rep) == 1


def test_legacy_placeholder_not_reported_as_tampering():
    """A pre-1.1.0 placeholder record (no seal_source, root == the placeholder
    hash) must be recognized, not reported as a season-root mismatch."""
    from orchard_chia.datalayer.attest import data_hash_for_uptime

    b = _bundle()
    b["meta"]["orchard_schema"] = "1.0.0"
    a = b["attest"]
    for k in ("seal_source", "sigs_verified", "root_mismatches"):
        a.pop(k, None)
    ph = data_hash_for_uptime(a["node_id"], int(a["season"]), int(a["hours_online"]))
    a["season_root"] = ph
    a["data_hash"] = ph
    rep = verify.verify_bundle(**b)
    names = {c.name for c in rep.checks}
    assert "Placeholder root well-formed" in names
    assert "Season root verified" not in names  # no false "tampered" report


def test_presence_counted_payment_is_labelled_not_changed():
    record = {"hours_online": 24, "verified_hours": 24,
              "seal_source": "readings", "sigs_verified": False}
    hours, basis = calculator.paid_hours(record)
    assert hours == 24  # amount unchanged
    assert basis == "verified_hours (sigs unchecked)"
    from orchard_chia.payout.main import _hours_cell
    assert _hours_cell({"hours": 24, "hours_basis": basis}) == "24 (sigs unchecked)"


# --- payout: honest basis, IDENTICAL amounts ------------------------------ #
def test_placeholder_payout_amount_is_unchanged_but_labelled():
    """The whole point: no unilateral change to what an operator is paid."""
    placeholder = {
        "hours_online": 24, "verified_hours": 0,
        "seal_source": schema.SEAL_SOURCE_PLACEHOLDER,
    }
    hours, basis = calculator.paid_hours(placeholder)
    assert hours == 24                      # same as the old laundered value
    assert basis == "hours_online (unverified)"
    # Amount identical to what the pre-1.1.0 laundering produced (24/24 * rate).
    assert calculator.juice_mojos_for_attestation(placeholder, daily_rate=1.0) == 1000


def test_proof_backed_still_pays_on_verified_hours():
    proven = {"hours_online": 24, "verified_hours": 12,
              "seal_source": "readings", "sigs_verified": True}
    assert calculator.paid_hours(proven) == (12, "verified_hours")
    assert calculator.juice_mojos_for_attestation(proven, daily_rate=1.0) == 500


def test_legacy_record_payout_amount_unchanged():
    """G3: a genuinely pre-1.1.0 record pays exactly what it always did. Only
    the basis LABEL changes, so the payer and a third-party verifier describe
    the same bytes the same way."""
    legacy = {"hours_online": 24, "verified_hours": 12}  # no seal_source
    hours, basis = calculator.paid_hours(legacy)
    assert hours == 12
    assert calculator.juice_mojos_for_attestation(legacy, daily_rate=1.0) == 500
    assert basis == "verified_hours (basis undeclared)"


def test_report_cell_marks_unverified_basis():
    from orchard_chia.payout.main import _hours_cell

    assert _hours_cell({
        "hours": 24, "hours_basis": "hours_online (unverified)", "claimed_hours": 24,
    }) == "24 (unverified)"
    # Proof-backed over-count annotation still works.
    assert _hours_cell({
        "hours": 12, "hours_basis": "verified_hours", "claimed_hours": 24,
    }) == "12 (claim 24)"
