# SPDX-License-Identifier: Apache-2.0
"""orchard-verify live must distinguish cannot-verify (exit 2) from INVALID
(exit 1). A transient/unprovable inclusion is not fraud."""
from __future__ import annotations

from orchard_chia.cli import orchard_verify as cli
from orchard_chia.datalayer import verify
from orchard_chia.datalayer.inclusion import InclusionReport


def _rep(inclusion_ok: bool, *, offline_ok: bool = True, incl_detail: str = "") -> verify.Report:
    checks = [verify.Check(cli.INCLUSION_CHECK_NAME, inclusion_ok, incl_detail)]
    checks.append(verify.Check("Device signature verified", offline_ok))
    return verify.Report(node_id="N", season=1, checks=checks)


def test_all_pass_exit_zero():
    rep = _rep(True, offline_ok=True)
    incl = InclusionReport(ok=True, detail="ok")
    assert cli._live_exit_code(rep, incl) == 0


def test_inclusion_cannot_verify_exit_two():
    rep = _rep(False, offline_ok=True)
    incl = InclusionReport(ok=False, detail="get_root failed", cannot_verify=True)
    assert cli._live_exit_code(rep, incl) == 2


def test_inclusion_value_mismatch_is_invalid_exit_one():
    rep = _rep(False, offline_ok=True)
    # value mismatch => definitive tampering => cannot_verify False
    incl = InclusionReport(ok=False, detail="value differs", cannot_verify=False)
    assert cli._live_exit_code(rep, incl) == 1


def test_offline_failure_is_invalid_even_if_inclusion_transient():
    # A bad signature is definitive; don't soften to cannot-verify.
    rep = _rep(False, offline_ok=False)
    incl = InclusionReport(ok=False, detail="get_root failed", cannot_verify=True)
    assert cli._live_exit_code(rep, incl) == 1


def test_unconfirmed_root_is_cannot_verify():
    rep = _rep(False, offline_ok=True)
    incl = InclusionReport(
        ok=False, detail="root not confirmed", confirmed=False, cannot_verify=True
    )
    assert cli._live_exit_code(rep, incl) == 2


def test_unsupported_scheme_is_cannot_verify():
    # The only failing offline check is the schema/scheme one, inclusion ok.
    rep = verify.Report(
        node_id="N", season=1,
        checks=[
            verify.Check(cli.INCLUSION_CHECK_NAME, True, ""),
            verify.Check("Schema and signer scheme supported", False, "ed25519"),
            verify.Check("Device signature verified", True, ""),
        ],
    )
    incl = InclusionReport(ok=True, detail="ok")
    assert cli._live_exit_code(rep, incl) == 2


def test_unanchored_reading_is_cannot_verify():
    # Current firmware sends placeholder anchors; an unanchored reading is
    # cannot-verify (anti-backdate can't be established), not fraud.
    rep = verify.Report(
        node_id="N", season=1,
        checks=[
            verify.Check(cli.INCLUSION_CHECK_NAME, True, ""),
            verify.Check("Anti-backdate anchor present", False, "placeholder"),
            verify.Check("Device signature verified", True, ""),
        ],
    )
    incl = InclusionReport(ok=True, detail="ok")
    assert cli._live_exit_code(rep, incl) == 2


def test_tampering_with_unsupported_scheme_still_invalid():
    # A definitive offline failure alongside the scheme one → INVALID wins.
    rep = verify.Report(
        node_id="N", season=1,
        checks=[
            verify.Check(cli.INCLUSION_CHECK_NAME, True, ""),
            verify.Check("Schema and signer scheme supported", False, ""),
            verify.Check("Device signature verified", False, "bad sig"),
        ],
    )
    incl = InclusionReport(ok=True, detail="ok")
    assert cli._live_exit_code(rep, incl) == 1
