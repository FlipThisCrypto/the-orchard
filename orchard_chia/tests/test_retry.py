# SPDX-License-Identifier: Apache-2.0
"""Retry / backoff policy for DataLayer RPC resilience."""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.retry import (
    RetryPolicy,
    call_with_retry,
    delay_for_attempt,
    is_transient_rpc_error,
)
from orchard_chia.datalayer.rpc import ChiaRpcError


def test_delay_grows_and_caps():
    pol = RetryPolicy(base_delay_s=1.0, max_delay_s=5.0, jitter=0.0)
    assert delay_for_attempt(1, pol) == 1.0
    assert delay_for_attempt(2, pol) == 2.0
    assert delay_for_attempt(3, pol) == 4.0
    assert delay_for_attempt(4, pol) == 5.0  # capped


def test_is_transient_classification():
    assert is_transient_rpc_error(ChiaRpcError("datalayer batch_update unreachable: x"))
    assert is_transient_rpc_error(ChiaRpcError("datalayer x -> 503: busy"))
    assert is_transient_rpc_error(ChiaRpcError("connection reset by peer"))
    assert not is_transient_rpc_error(
        ChiaRpcError("refusing verify=False against non-loopback host")
    )
    assert not is_transient_rpc_error(
        ChiaRpcError("datalayer batch_update returned success=false: {}")
    )
    assert not is_transient_rpc_error(ChiaRpcError("datalayer x -> 404: missing"))
    # Rate-limit and request-timeout are transient (retry-after conditions).
    assert is_transient_rpc_error(ChiaRpcError("datalayer x -> 429: slow down"))
    assert is_transient_rpc_error(ChiaRpcError("datalayer x -> 408: request timeout"))
    # Other 4xx still permanent.
    assert not is_transient_rpc_error(ChiaRpcError("datalayer x -> 400: bad request"))
    assert not is_transient_rpc_error(ChiaRpcError("datalayer x -> 409: conflict"))


def test_call_with_retry_succeeds_after_transient(monkeypatch):
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ChiaRpcError("datalayer batch_update unreachable: down")
        return {"success": True, "tx_id": "abc"}

    retries: list[int] = []

    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_attempts=4, base_delay_s=0.01, jitter=0.0),
        sleep=lambda s: sleeps.append(s),
        on_retry=lambda a, e, w: retries.append(a),
    )
    assert result.value["tx_id"] == "abc"
    assert result.attempts == 3
    assert result.retried is True
    assert len(sleeps) == 2
    assert retries == [1, 2]


def test_call_with_retry_does_not_retry_permanent():
    calls = {"n": 0}

    def permanent():
        calls["n"] += 1
        raise ChiaRpcError("datalayer batch_update returned success=false: bad")

    with pytest.raises(ChiaRpcError, match="success=false"):
        call_with_retry(
            permanent,
            policy=RetryPolicy(max_attempts=5, base_delay_s=0.01, jitter=0.0),
            sleep=lambda _s: None,
        )
    assert calls["n"] == 1


def test_call_with_retry_exhausts_attempts():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise ChiaRpcError("datalayer x unreachable: nope")

    with pytest.raises(ChiaRpcError, match="unreachable"):
        call_with_retry(
            always,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0),
            sleep=lambda _s: None,
        )
    assert calls["n"] == 3
