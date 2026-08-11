# SPDX-License-Identifier: Apache-2.0
"""A season with no evidence is not sealed at all.

A placeholder attestation declares that nothing was published for a season. It
is unpayable by construction (payout/calculator.py, iteration 3), it proves
nothing to a verifier, and writing it costs a blockchain fee for a permanent,
public statement of absence.

185 of them are on the live store. They are the reason the store implies 4,081
hours of uptime against 9 hours of real sensor data.

Skipping is the default. The opt-in exists because an operator who wants a
marker that a season existed may reasonably want one — but that is their
decision to make explicitly, not a default to inherit.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import main as attest_main
from orchard_chia.datalayer.main import (ATTEST_WRITE_PLACEHOLDERS_ENV,
                                         _write_placeholders)


def test_placeholders_are_skipped_by_default(monkeypatch):
    monkeypatch.delenv(ATTEST_WRITE_PLACEHOLDERS_ENV, raising=False)
    assert _write_placeholders() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_the_opt_in_is_accepted_in_the_obvious_forms(monkeypatch, value):
    monkeypatch.setenv(ATTEST_WRITE_PLACEHOLDERS_ENV, value)
    assert _write_placeholders() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "y"])
def test_anything_else_means_no(monkeypatch, value):
    """Fail closed. A typo in a scheduler's environment must not start writing
    fee-bearing records that prove nothing."""
    monkeypatch.setenv(ATTEST_WRITE_PLACEHOLDERS_ENV, value)
    assert _write_placeholders() is False


def test_it_is_not_a_cli_flag():
    """attest refuses every option on purpose: a --dry-run that did not exist
    was once silently ignored and wrote 185 records to the chain. Re-adding a
    flag surface here would rebuild the trap."""
    import subprocess
    import sys
    r = subprocess.run(
        [sys.executable, "-m", "orchard_chia.datalayer", "attest",
         "--write-placeholders"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "attest takes no options" in (r.stderr + r.stdout)


def test_the_env_name_is_explicit_about_what_it_does():
    assert "PLACEHOLDER" in ATTEST_WRITE_PLACEHOLDERS_ENV
    assert ATTEST_WRITE_PLACEHOLDERS_ENV.startswith("ORCHARD_")


def test_the_writer_counts_what_it_skipped():
    """A silent skip is indistinguishable from a season that was never
    considered. The stat is how an operator sees the difference."""
    import inspect
    src = inspect.getsource(attest_main)
    assert 'stats["placeholder_skipped"]' in src
    assert "print(f\"[orchard.attest] stats:" in src
