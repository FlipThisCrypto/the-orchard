# SPDX-License-Identifier: Apache-2.0
"""The indexer must identify itself, or every Pass verification fails.

MintGarden's edge blocks urllib's default ``Python-urllib/3.x`` User-Agent.
Because Pass verification gates wallet binding at registration, that single
missing header meant nobody could bind a wallet to a Tree — the failure surfaced
as "Could not verify Orchard Pass: indexer error: MintGarden … -> HTTP 403".

Diagnosed by reproducing it from an unrelated IP: same URL, same library,
default UA -> 403, a real User-Agent -> 200. So it was the header, not the
address, not rate limiting, and not the indexer being down.
"""
from __future__ import annotations

import urllib.error

import pytest

from orchard_chia.nft import verify


def test_a_user_agent_is_declared():
    assert verify.USER_AGENT, "the indexer must have a User-Agent to send"
    assert "urllib" not in verify.USER_AGENT.lower(), (
        "the default urllib UA is exactly what MintGarden blocks"
    )
    # Identify who we are and give them somewhere to look.
    assert "orchard" in verify.USER_AGENT.lower()


def test_the_request_actually_carries_it(monkeypatch):
    """A constant nobody sends fixes nothing — assert it reaches the wire."""
    seen: dict = {}

    class FakeResponse:
        def read(self):
            return b'{"items": []}'

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    verify._fetch_mintgarden_collection_items()

    assert seen["ua"] == verify.USER_AGENT, (
        "the request went out without our User-Agent — MintGarden will 403 it"
    )
    assert seen["url"].startswith(verify.MINTGARDEN_API_BASE)


def test_a_403_says_what_to_check(monkeypatch):
    # The original message was just "-> HTTP 403", which sent me looking at IP
    # blocking and rate limits. Naming the likely cause saves that hour.
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(verify.urllib.request, "urlopen", boom)
    with pytest.raises(verify.IndexerError) as ei:
        verify._fetch_mintgarden_collection_items()
    assert "403" in str(ei.value)
    assert "User-Agent" in str(ei.value)


def test_other_statuses_are_not_given_the_403_hint(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

    monkeypatch.setattr(verify.urllib.request, "urlopen", boom)
    with pytest.raises(verify.IndexerError) as ei:
        verify._fetch_mintgarden_collection_items()
    assert "500" in str(ei.value)
    assert "User-Agent" not in str(ei.value), "a 500 is not a UA problem"
