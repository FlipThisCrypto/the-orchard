# SPDX-License-Identifier: Apache-2.0
"""sync-oracle: the chain is the authority, the oracle's table is a cache.

The divergence this closes was real and silent: season 76 sealed on chain and
verified VALID while /network/stats reported nothing newer than two days
earlier, because attest's POST-back had failed once and the writer — correctly
— never re-seals an already-sealed season, so it never retried.
"""
from __future__ import annotations

import json

import pytest

from orchard_chia.datalayer import sync_oracle


ATTEST = {
    "node_id": "D8641AD6CAE36977818499469F7E8C49",
    "season": 76,
    "hours_online": 9,
    "verified_hours": 9,
    "data_hash": "e6" * 32,
    "oracle_sig": "66" * 64,
    "block_height_at_write": 9133109,
    "signed_at": "2026-08-11T00:00:00Z",
}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sync_oracle.requests.HTTPError(f"HTTP {self.status_code}")


def test_the_posted_body_carries_what_the_chain_proves():
    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.update(json)
        return _Resp(201, {"hours_match": True})

    ok, detail = _post_with(fake_post)
    assert ok and detail == "synced"
    assert sent["season_number"] == 76
    assert sent["hours_online"] == 9
    assert sent["data_hash"] == ATTEST["data_hash"]
    assert sent["oracle_sig"] == ATTEST["oracle_sig"]
    assert sent["block_height_at_write"] == 9133109


def test_the_transaction_id_is_null_not_invented():
    """A sealed record does not name the transaction that placed it. A
    fabricated id would be worse than an absent one — it would look
    checkable."""
    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.update(json)
        return _Resp(201, {"hours_match": True})

    _post_with(fake_post)
    assert sent["dl_tx_id"] is None


def test_the_write_time_is_deterministic_so_re_running_is_a_no_op():
    """Wall-clock now() would make every re-run rewrite the row with a new
    timestamp. signed_at is the season's own close boundary."""
    bodies = []

    def fake_post(url, json=None, timeout=None, headers=None):
        bodies.append(dict(json))
        return _Resp(201, {"hours_match": True})

    _post_with(fake_post)
    _post_with(fake_post)
    assert bodies[0] == bodies[1]
    assert bodies[0]["written_to_datalayer_at"] == "2026-08-11T00:00:00Z"


def test_an_hours_mismatch_is_reported_loudly_but_still_synced():
    """The chain record IS what is published. Withholding it from the oracle
    would not make a disagreement less true — it would only hide it."""
    def fake_post(url, json=None, timeout=None, headers=None):
        return _Resp(200, {"hours_match": False, "oracle_hours_online": 7})

    ok, detail = _post_with(fake_post)
    assert ok is True
    assert "MISMATCH" in detail
    assert "9h" in detail and "7h" in detail


def test_a_refused_post_is_a_failure_with_the_reason():
    def fake_post(url, json=None, timeout=None, headers=None):
        return _Resp(403, None, text="writer token required")

    ok, detail = _post_with(fake_post)
    assert ok is False
    assert "403" in detail and "writer token" in detail


def test_a_network_error_is_a_failure_not_a_crash():
    def fake_post(url, json=None, timeout=None, headers=None):
        raise sync_oracle.requests.ConnectionError("connection refused")

    ok, detail = _post_with(fake_post)
    assert ok is False
    assert "refused" in detail


def test_the_writer_token_is_presented_when_configured():
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen["headers"] = headers
        return _Resp(201, {"hours_match": True})

    _post_with(fake_post, token="s3cret")
    assert seen["headers"]["X-Orchard-Writer-Token"] == "s3cret"


def test_the_token_never_reaches_the_body_or_the_url():
    """A token in a URL lands in access logs and referrers."""
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen["url"] = url
        seen["body"] = json
        return _Resp(201, {"hours_match": True})

    _post_with(fake_post, token="s3cret")
    assert "s3cret" not in seen["url"]
    assert "s3cret" not in json_dumps(seen["body"])


def test_known_seasons_reads_the_oracles_own_list(monkeypatch):
    monkeypatch.setattr(sync_oracle.requests, "get",
                        lambda *a, **k: _Resp(200, [{"season_number": 74},
                                                    {"season_number": 75}]))
    assert sync_oracle._known_seasons("http://o", "NODE", None) == {74, 75}


def test_a_non_list_attestations_response_is_refused_not_guessed(monkeypatch):
    """Treating an error object as "no seasons known" would re-POST every
    season on every run."""
    monkeypatch.setattr(sync_oracle.requests, "get",
                        lambda *a, **k: _Resp(200, {"detail": "nope"}))
    with pytest.raises(ValueError, match="expected a list"):
        sync_oracle._known_seasons("http://o", "NODE", None)


# --- helpers ---------------------------------------------------------------

def json_dumps(obj):
    return json.dumps(obj, default=str)


def _post_with(fake_post, token=None):
    import unittest.mock as _m
    with _m.patch.object(sync_oracle.requests, "post", fake_post):
        return sync_oracle._post("http://oracle.test", ATTEST, "6174", token)
