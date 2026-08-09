# SPDX-License-Identifier: Apache-2.0
"""A command that writes to a blockchain must never ignore its arguments.

``attest`` takes no options, and the dispatcher called ``main()`` with none —
so any flag was silently discarded while the job ran for real. On 2026-08-08
``attest --dry-run`` therefore performed a live batch_update, paid a fee, and
wrote 185 permanent records into a public store (tx 0x583aa051…). The operator
had every reason to expect a rehearsal: ``--dry-run`` is real on ``publish``,
and the two commands sit one word apart in the same CLI.

Silently ignoring input is the worst of the available behaviours — from the
caller's side it is indistinguishable from honouring it. For an irreversible,
fee-paying, permanent-record operation it has to refuse.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.__main__ import _dispatch


@pytest.mark.parametrize("flag", ["--dry-run", "--dryrun", "-n", "--check", "--please-dont"])
def test_attest_refuses_any_flag_rather_than_ignoring_it(flag, capsys):
    assert _dispatch(["attest", flag]) == 2
    err = capsys.readouterr().err
    assert "takes no options" in err
    assert flag in err, "the message must name what it refused"


def test_the_refusal_says_why_and_what_to_do_instead():
    """A refusal that leaves you stuck gets worked around."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _dispatch(["attest", "--dry-run"])
    err = buf.getvalue()
    assert "blockchain" in err and "cannot be undone" in err, "say why it matters"
    assert "reconcile" in err, "offer the read-only alternative"


@pytest.mark.parametrize("alias", ["attest", "attestation", "season"])
def test_every_attest_alias_is_guarded(alias, capsys):
    # The aliases are the trap: someone typing `season --dry-run` deserves the
    # same protection as `attest --dry-run`.
    assert _dispatch([alias, "--dry-run"]) == 2


def test_bare_attest_is_still_dispatched(monkeypatch):
    """The guard must not break the command it protects."""
    called = {"n": 0}

    def fake_main():
        called["n"] += 1
        return 0

    import orchard_chia.datalayer.main as m
    monkeypatch.setattr(m, "main", fake_main)
    assert _dispatch(["attest"]) == 0
    assert called["n"] == 1


def test_subcommands_that_DO_take_flags_still_receive_them(monkeypatch):
    """The fix must not become a blanket ban on arguments."""
    seen = {}

    import orchard_chia.datalayer.publish as p
    import orchard_chia.datalayer.preflight as pf
    import orchard_chia.datalayer.reconcile as rc
    monkeypatch.setattr(p, "main", lambda argv: seen.setdefault("publish", argv) and 0 or 0)
    monkeypatch.setattr(pf, "main", lambda argv: seen.setdefault("preflight", argv) and 0 or 0)
    monkeypatch.setattr(rc, "main", lambda argv: seen.setdefault("reconcile", argv) and 0 or 0)

    _dispatch(["publish", "--dry-run", "--lookback-hours", "4"])
    _dispatch(["preflight", "--skip-chia"])
    _dispatch(["reconcile", "--season", "74"])

    assert seen["publish"] == ["--dry-run", "--lookback-hours", "4"]
    assert seen["preflight"] == ["--skip-chia"]
    assert seen["reconcile"] == ["--season", "74"]


def test_an_unknown_subcommand_is_still_refused(capsys):
    assert _dispatch(["destroy-everything"]) == 2
