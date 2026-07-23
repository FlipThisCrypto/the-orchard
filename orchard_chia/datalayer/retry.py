# SPDX-License-Identifier: Apache-2.0
"""Bounded exponential backoff for transient Chia DataLayer RPC failures.

Publish/attest must survive brief wallet/DataLayer restarts and network
blips without the operator re-running cron by hand. Permanent errors
(bad store id, success=false business rejects) are not retried.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    jitter: float = 0.25  # fraction of delay

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return max(1, int(raw))
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return max(0.0, float(raw))
            except ValueError:
                return default

        return cls(
            max_attempts=_int("ORCHARD_DL_RPC_MAX_ATTEMPTS", 4),
            base_delay_s=_float("ORCHARD_DL_RPC_BASE_DELAY_S", 0.5),
            max_delay_s=_float("ORCHARD_DL_RPC_MAX_DELAY_S", 30.0),
            jitter=_float("ORCHARD_DL_RPC_JITTER", 0.25),
        )


def delay_for_attempt(attempt: int, policy: RetryPolicy) -> float:
    """Sleep seconds before attempt ``attempt`` (1-based after a failure).

    attempt=1 → base, attempt=2 → 2*base, capped at max_delay, with jitter.
    """
    if attempt < 1:
        attempt = 1
    raw = policy.base_delay_s * (2 ** (attempt - 1))
    capped = min(raw, policy.max_delay_s)
    if policy.jitter <= 0:
        return capped
    span = capped * policy.jitter
    return max(0.0, capped + random.uniform(-span, span))


def is_transient_rpc_error(exc: BaseException) -> bool:
    """Heuristic: transport / timeout / 5xx-style messages are transient.

    Permanent client errors (4xx in message, success=false, non-loopback
    refusal) are not retried.
    """
    msg = str(exc).lower()
    if "refusing verify=false" in msg:
        return False
    if "success=false" in msg:
        return False
    # HTTP status in our ChiaRpcError strings: "-> 400", "-> 404", etc.
    for code in ("-> 400", "-> 401", "-> 403", "-> 404", "-> 409", "-> 422"):
        if code in msg:
            return False
    if any(s in msg for s in (
        "unreachable",
        "timeout",
        "timed out",
        "connection",
        "reset",
        "temporarily",
        "-> 408",  # request timeout — retry
        "-> 429",  # rate limited (e.g. behind a reverse proxy) — back off + retry
        "-> 500",
        "-> 502",
        "-> 503",
        "-> 504",
    )):
        return True
    # Default: treat unknown RuntimeError subclasses as transient only if
    # the message looks network-y; otherwise do not retry forever.
    return "rpc" in msg and "unreachable" in msg


@dataclass
class RetryResult:
    value: T
    attempts: int
    retried: bool


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    is_transient: Callable[[BaseException], bool] = is_transient_rpc_error,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> RetryResult[T]:
    """Invoke ``fn`` until success or attempts exhausted.

    Raises the last exception if all attempts fail.
    """
    pol = policy or RetryPolicy.from_env()
    last: BaseException | None = None
    for attempt in range(1, pol.max_attempts + 1):
        try:
            value = fn()
            return RetryResult(
                value=value,
                attempts=attempt,
                retried=attempt > 1,
            )
        except Exception as e:  # noqa: BLE001 — classified below
            last = e
            if attempt >= pol.max_attempts or not is_transient(e):
                raise
            wait = delay_for_attempt(attempt, pol)
            if on_retry is not None:
                on_retry(attempt, e, wait)
            sleep(wait)
    assert last is not None
    raise last
