# SPDX-License-Identifier: Apache-2.0
"""The superseded payout CLI cannot spend by muscle memory.

Two payment paths existed after the economics ratification: the new one, and
python -m orchard_chia.payout --yes — deprecated in a docstring, which stops
nobody. A habitual run would have paid under the unbounded per-Tree model.
Reports and dry runs stay available for reconciling history; spending needs an
acknowledgement no one types by accident.
"""
from __future__ import annotations

import inspect


def test_the_spend_gate_names_the_current_model():
    from orchard_chia.payout import main as payout_main
    src = inspect.getsource(payout_main)
    assert "ORCHARD_PAYOUT_SUPERSEDED_MODEL_ACK" in src
    assert "orchard_chia.economics pay" in src


def test_the_ack_value_is_not_a_boolean():
    """"1" or "true" can land in an environment by accident; "i-know" cannot."""
    from orchard_chia.payout import main as payout_main
    src = inspect.getsource(payout_main)
    assert '"i-know"' in src


def test_the_gate_sits_before_the_dry_run_short_circuit():
    """The refusal must fire on --yes BEFORE anything is signed or sent, and
    plain runs (no flags) must still reach the dry-run report path."""
    from orchard_chia.payout import main as payout_main
    src = inspect.getsource(payout_main)
    gate = src.index("ORCHARD_PAYOUT_SUPERSEDED_MODEL_ACK")
    dry = src.index("DRY RUN (re-run with --confirm")
    assert gate < dry


# --- the allocation CLI is disarmed the same way ---------------------------

def test_the_allocation_spend_gate_names_the_current_model():
    from orchard_chia.allocation import __main__ as alloc_main
    src = inspect.getsource(alloc_main)
    assert "ORCHARD_ALLOCATION_SUPERSEDED_MODEL_ACK" in src
    assert "orchard_chia.economics" in src


def test_a_live_allocation_run_is_refused_without_the_ack(monkeypatch, capsys):
    """The two-act rule plus the supersession ack: three deliberate steps to
    spend under a dead model, zero to report under it."""
    from orchard_chia.allocation.__main__ import main
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ORCHARD_ALLOCATION_SUPERSEDED_MODEL_ACK", raising=False)
    rc = main(["run", "--i-understand-this-spends-real-tokens"])
    assert rc == 2
    assert "SUPERSEDED wallet-mean model" in capsys.readouterr().err


def test_allocation_reports_still_run(monkeypatch, tmp_path, capsys):
    from orchard_chia.allocation.__main__ import main
    monkeypatch.setenv("ORCHARD_ALLOC_DB", str(tmp_path / "a.db"))
    monkeypatch.delenv("DRY_RUN", raising=False)
    rc = main(["history"])
    assert rc == 0
