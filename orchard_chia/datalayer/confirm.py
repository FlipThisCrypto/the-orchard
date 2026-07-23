# SPDX-License-Identifier: Apache-2.0
"""Post-write confirmation for DataLayer batch_update results.

A successful RPC response is not enough under load: the service can accept
a batch while the local view of the tree lags, or return success after a
partial apply. Confirming insert values via ``get_value`` turns silent
corruption into a loud, retried failure (convergent writers re-run).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SupportsGetValue(Protocol):
    def get_value(self, store_id: str, key_hex: str) -> str | None: ...


@dataclass(frozen=True)
class ConfirmResult:
    ok: bool
    checked: int
    mismatched: list[str]  # ascii keys or key hex prefixes
    missing: list[str]
    detail: str


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
