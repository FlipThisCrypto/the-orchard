# SPDX-License-Identifier: Apache-2.0
"""SPEC §8 verification badge mapping."""
from __future__ import annotations

from orchard_chia.datalayer import verify


def _report(valid: bool) -> verify.Report:
    return verify.Report(
        node_id="N", season=1,
        checks=[verify.Check("x", valid, "")],
    )


def test_verified_when_valid_and_sealed():
    assert verify.verification_badge(_report(True), sealed=True) == "Verified"


def test_live_when_valid_and_not_sealed():
    assert verify.verification_badge(_report(True), sealed=False) == "Live"


def test_partial_when_a_check_failed():
    assert verify.verification_badge(_report(False)) == "Partial"


def test_stale_takes_over_a_valid_report():
    assert verify.verification_badge(_report(True), stale=True) == "Stale"


def test_unverifiable_takes_precedence():
    # Even a stale/valid situation is 'Unverified' when nothing could be proven.
    assert verify.verification_badge(
        _report(True), stale=True, unverifiable=True
    ) == "Unverified"
    assert verify.verification_badge(
        _report(False), unverifiable=True
    ) == "Unverified"
