# SPDX-License-Identifier: Apache-2.0
"""Post-write confirmation for DataLayer batch_update results.

A successful RPC response is not enough under load: the service can accept
a batch while the local view of the tree lags, or return success after a
partial apply. Confirming insert values via ``get_value`` turns silent
corruption into a loud, retried failure (convergent writers re-run).

BUT a write is not readable the instant the RPC returns. ``batch_update``
submits a transaction; the store's root only moves once that transaction is
in a block, roughly one to three ~52-second blocks later. Reading the value
back immediately therefore finds it missing on a write that succeeded
perfectly.

That is not theoretical. The first real publish to the Orchard's store
(2026-08-08, tx 0xa3577121…) was accepted, sat in the mempool, and this module
reported ``missing=['meta:schema']`` and refused to advance the watermark. The
root advanced and confirmed a minute later and the value read back exactly as
written. The check cried wolf on a completely successful publish — and it would
have done so on EVERY successful publish, not just slow ones.

Worse, the advice it printed ("re-run to converge") was actively harmful while
the transaction was pending: a re-run reads the store, still sees the old
value, plans the same write again, and submits a SECOND transaction — paying
the fee twice for one update.

So three outcomes are distinguished here, not two:

  ok       the write is on chain and every sampled value matches.
  pending  the write was accepted and has not confirmed yet. Nothing is
           wrong. Do not re-run until it confirms.
  failed   the root moved and the values are missing or wrong. Something
           really is broken.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol


class SupportsGetValue(Protocol):
    def get_value(self, store_id: str, key_hex: str) -> str | None: ...


class SupportsGetRoot(Protocol):
    def get_root(self, store_id: str) -> dict: ...


# One Chia block is ~52s and a transaction usually lands within a few. Waiting
# is the correct behaviour rather than exiting and re-running, because a re-run
# before confirmation duplicates the transaction and the fee.
DEFAULT_WAIT_S = 300.0
DEFAULT_POLL_S = 15.0


@dataclass(frozen=True)
class ConfirmResult:
    ok: bool
    checked: int
    mismatched: list[str]  # ascii keys or key hex prefixes
    missing: list[str]
    detail: str
    pending: bool = False  # accepted on chain, not yet confirmed — NOT a failure


def wait_for_root(
    rpc: SupportsGetRoot,
    store_id: str,
    *,
    root_before: str | None,
    timeout_s: float = DEFAULT_WAIT_S,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    """Block until the store's root has moved past ``root_before`` and is
    confirmed. Returns (confirmed, detail).

    ``root_before`` is the root hash read immediately BEFORE the write. Waiting
    for the hash to *change* — not merely for ``confirmed`` to be true — is what
    makes this correct: the pre-write root is already confirmed, so polling for
    ``confirmed`` alone would return instantly and prove nothing.
    """
    deadline = monotonic() + timeout_s
    last = "no root read"
    while True:
        try:
            root = rpc.get_root(store_id)
        except Exception as e:  # an RPC blip must not read as a failed write
            last = f"get_root failed: {e}"
            root = None
        if isinstance(root, dict):
            hash_now = root.get("hash")
            confirmed = bool(root.get("confirmed"))
            # A missing baseline must never count as movement: that made a
            # pre-write RPC blip indistinguishable from a landed write, and
            # the OLD root is confirmed too. Without a baseline the only
            # honest answer is "cannot confirm" — the caller retries with a
            # baseline or treats the write as pending.
            moved = bool(root_before) and bool(hash_now) and hash_now != root_before
            if confirmed and moved:
                return True, f"root advanced to {str(hash_now)[:18]}… and confirmed"
            last = (
                f"root={str(hash_now)[:18]}… confirmed={confirmed} "
                f"moved={bool(moved)}"
            )
        if monotonic() >= deadline:
            return False, f"still unconfirmed after {timeout_s:.0f}s ({last})"
        sleep(min(poll_s, max(0.0, deadline - monotonic())))


def inserts_from_changelist(changelist: list[dict]) -> list[tuple[str, str]]:
    """Extract final (key_hex, value_hex) pairs after delete/insert pairs.

    For a key that is deleted then inserted, only the insert value matters.
    """
    final: dict[str, str] = {}
    for item in changelist:
        action = item.get("action")
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if action == "delete":
            final.pop(key, None)
        elif action == "insert":
            val = item.get("value")
            if isinstance(val, str):
                final[key] = val
    return list(final.items())


def confirm_inserts(
    rpc: SupportsGetValue,
    store_id: str,
    inserts: list[tuple[str, str]],
    *,
    max_checks: int | None = None,
) -> ConfirmResult:
    """Verify up to ``max_checks`` insert pairs via get_value.

    Checks a prefix of the insert list (stable order) to bound RPC cost
    on large batches while still catching systematic apply failures.
    """
    if not inserts:
        return ConfirmResult(
            ok=True,
            checked=0,
            mismatched=[],
            missing=[],
            detail="no inserts to confirm",
        )

    import os
    if max_checks is None:
        try:
            max_checks = int(os.environ.get('ORCHARD_DL_CONFIRM_MAX', '32') or '32')
        except ValueError:
            max_checks = 32
    sample = _spread_sample(inserts, max(1, int(max_checks)))
    missing: list[str] = []
    mismatched: list[str] = []

    for key_hex, expected in sample:
        label = _key_label(key_hex)
        got = rpc.get_value(store_id, key_hex)
        if got is None:
            missing.append(label)
        elif got != expected:
            mismatched.append(label)

    checked = len(sample)
    ok = not missing and not mismatched
    if ok:
        detail = f"confirmed {checked}/{len(inserts)} insert(s)"
    else:
        detail = (
            f"confirm failed: missing={len(missing)} "
            f"mismatched={len(mismatched)} of {checked} checked"
        )
    return ConfirmResult(
        ok=ok,
        checked=checked,
        mismatched=mismatched,
        missing=missing,
        detail=detail,
    )


def confirm_after_write(
    rpc,
    store_id: str,
    inserts: list[tuple[str, str]],
    *,
    root_before: str | None,
    max_checks: int | None = None,
    timeout_s: float = DEFAULT_WAIT_S,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ConfirmResult:
    """Wait for the write to reach a block, THEN verify the values.

    This is the entry point callers should use after ``batch_update``. Reading
    values back without waiting reports a perfectly good write as missing —
    see the module docstring for the publish that proved it.
    """
    if not inserts:
        return ConfirmResult(True, 0, [], [], "no inserts to confirm")

    confirmed, why = wait_for_root(
        rpc,
        store_id,
        root_before=root_before,
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not confirmed:
        # Accepted but not in a block yet. The write is almost certainly fine;
        # what is NOT fine is re-running, which would submit it a second time
        # and pay the fee twice.
        return ConfirmResult(
            ok=False,
            checked=0,
            mismatched=[],
            missing=[],
            detail=f"write submitted but not yet confirmed — {why}",
            pending=True,
        )
    return confirm_inserts(rpc, store_id, inserts, max_checks=max_checks)


def _spread_sample(inserts: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
    """Up to ``n`` inserts spread evenly across the batch (not just the prefix),
    always including the last one, so a systematic apply-failure in the tail is
    caught — at the same RPC cost as a prefix sample.
    """
    total = len(inserts)
    if total <= n:
        return list(inserts)
    step = total / n
    idxs = {int(i * step) for i in range(n)}
    idxs.add(total - 1)  # always check the final insert
    return [inserts[i] for i in sorted(idxs)]


def _key_label(key_hex: str) -> str:
    try:
        return bytes.fromhex(key_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return key_hex[:20] + "…"
