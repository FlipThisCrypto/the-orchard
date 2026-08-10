# SPDX-License-Identifier: Apache-2.0
"""Replay protection is ON unless someone turns it off.

require_seq defaulted to False, which meant seq was tracked but a replayed
reading was silently accepted — it stored a row, bumped uptime_hours, and
since hours are heartbeats are $JUICE, a captured reading replayed 30 times
minted a credited hour. The firmware has sent a monotonic NVS-persisted seq
since the watermark landed, and every seq-less legacy node is retired, so
enforcement costs a healthy Tree nothing.
"""
from __future__ import annotations


def test_the_default_is_enforcement(monkeypatch):
    monkeypatch.delenv("ORCHARD_ORACLE_REQUIRE_SEQ", raising=False)
    from oracle.app.config import Settings
    assert Settings(db_url="sqlite:///:memory:").require_seq is True


def test_switching_it_off_is_an_explicit_act(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_SEQ", "false")
    from oracle.app.config import Settings
    assert Settings(db_url="sqlite:///:memory:").require_seq is False
