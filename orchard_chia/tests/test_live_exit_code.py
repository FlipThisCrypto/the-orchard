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
