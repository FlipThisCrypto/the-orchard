# SPDX-License-Identifier: Apache-2.0
"""Transaction executor — the only module in this package that can move funds.

It is deliberately the dumbest component. Every decision was made upstream: by
the time an instruction arrives here it has already survived the pause switch,
the in-flight check, the per-cycle and per-wallet caps, the dust floor and the
balance check. The executor's whole job is to send what it was given, record
what happened, and never guess.

CREDENTIALS
===========

Signing authority comes from the Chia wallet daemon, reached over mTLS with
cert and key paths supplied by config or environment. This module never reads a
mnemonic, never holds a private key, never logs a path's contents, and never
places a secret in an error message, a memo, or an audit row. The key material
stays where the wallet put it.

RETRIES, AND THE ONE THING THAT IS NOT RETRIED
==============================================

Failures before a transaction is accepted are retried with exponential backoff:
a wallet that is briefly busy or a daemon still syncing is a transient
condition, and giving up on it turns a hiccup into a missed payout.

A failure AFTER the RPC was issued is never retried. If the call timed out, or
the process died, or the daemon answered something unparseable, then whether
the spend went out is unknown — and "unknown" is not "no". The instruction is
left in ``sending``, the run stops, and a human looks at the wallet. Retrying
there is how you pay twice, and a CAT spend cannot be recalled.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import audit as audit_mod
from .planner import SpendInstruction, SpendPlan


class ExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionReport:
    cycle_id: str
    dry_run: bool
    sent: tuple[tuple[str, int, str], ...] = ()      # (wallet, mojos, tx_id)
    failed: tuple[tuple[str, int, str], ...] = ()    # (wallet, mojos, error)
    skipped: tuple[tuple[str, int, str], ...] = ()
    halted_reason: str | None = None

    @property
    def sent_mojos(self) -> int:
        return sum(m for _, m, _ in self.sent)

    @property
    def ok(self) -> bool:
        return not self.failed and self.halted_reason is None


def build_spender(*, wallet_id: int, fee_mojos: int, wallet_cfg: dict,
                  rpc_factory=None) -> "WalletSpender":
    """The one place a live spender is constructed.

    Two CLIs used to wire this independently; one passed ca_cert_path /
    ca_key_path keywords WalletRpc does not accept, so every live run would
    have crashed with a TypeError at the exact moment an operator first went
    live — the path no dry run ever exercises. One builder, matching the real
    signature, testable via rpc_factory.
    """
    if not wallet_id:
        raise ExecutorError("a live spender needs a wallet_id")
    missing = [k for k in ("cert_path", "key_path") if not wallet_cfg.get(k)]
    if missing:
        raise ExecutorError(
            f"wallet config is missing {', '.join(missing)} — a live payment "
            f"cannot reach the wallet daemon without its mTLS credentials")
    if rpc_factory is None:
        from ..wallet.rpc import WalletRpc
        rpc_factory = WalletRpc
    rpc = rpc_factory(
        host=wallet_cfg.get("host", "localhost"),
        port=int(wallet_cfg.get("port", 9256)),
        cert_path=wallet_cfg["cert_path"],
        key_path=wallet_cfg["key_path"],
        fingerprint=int(wallet_cfg.get("fingerprint", 0)),
    )
    return WalletSpender(rpc, wallet_id=wallet_id, fee_mojos=fee_mojos)


class WalletSpender:
    """Thin adapter over the project's existing Chia wallet RPC.

    Kept as a class with one method so tests can substitute a fake without
    monkeypatching the network, and so the real credential handling lives in
    exactly one place.
    """

    def __init__(self, rpc, wallet_id: int, fee_mojos: int = 0):
        self._rpc = rpc
        self.wallet_id = wallet_id
        self.fee_mojos = fee_mojos

    def spendable_balance(self) -> int | None:
        """CAT mojos available. None when the wallet cannot answer.

        None means "unknown", and the planner treats unknown as unusable rather
        than as unlimited — a balance check that silently passes when it cannot
        run is not a balance check.
        """
        try:
            got = self._rpc._post("get_wallet_balance", {"wallet_id": self.wallet_id})
        except Exception:
            return None
        bal = (got or {}).get("wallet_balance") or {}
        v = bal.get("spendable_balance", bal.get("confirmed_wallet_balance"))
        return int(v) if isinstance(v, (int, float)) else None

    def send(self, instruction: SpendInstruction) -> str:
        got = self._rpc.cat_spend(
            wallet_id=self.wallet_id,
            inner_address=instruction.wallet_address,
            amount=instruction.amount_mojos,
            fee=self.fee_mojos,
            memos=[instruction.memo],
        )
        tx = (got or {}).get("transaction_id") or ((got or {}).get("transaction") or {}).get("name")
        if not tx:
            raise ExecutorError(
                "wallet accepted the spend but returned no transaction_id; "
                "treating as UNKNOWN rather than failed")
        return str(tx)

    def confirmed(self, tx_id: str) -> bool:
        try:
            got = self._rpc._post("get_transaction", {"transaction_id": tx_id})
        except Exception:
            return False
        return bool(((got or {}).get("transaction") or {}).get("confirmed"))


def execute(
    plan: SpendPlan,
    *,
    store: audit_mod.AuditStore,
    spender: WalletSpender | None,
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
    sleep=time.sleep,
) -> ExecutionReport:
    """Send a plan. In dry-run nothing is sent and nothing is signed."""
    if plan.blocked_by:
        store.event("refused", cycle_id=plan.cycle_id, reasons=list(plan.blocked_by))
        return ExecutionReport(cycle_id=plan.cycle_id, dry_run=plan.dry_run,
                               halted_reason="; ".join(plan.blocked_by))

    if plan.dry_run:
        store.event("dry-run", cycle_id=plan.cycle_id,
                    would_send=plan.total_mojos, wallets=len(plan.instructions))
        store.set_cycle_state(plan.cycle_id, "dry-run")
        return ExecutionReport(
            cycle_id=plan.cycle_id, dry_run=True,
            skipped=tuple((i.wallet_address, i.amount_mojos, "dry run")
                          for i in plan.instructions))

    if spender is None:
        raise ExecutorError("a live run needs a WalletSpender; refusing to "
                            "proceed without one")

    sent, failed, skipped = [], [], []
    halted = None
    store.set_cycle_state(plan.cycle_id, "sending")

    for ins in plan.instructions:
        if store.already_paid(plan.cycle_id, ins.wallet_address):
            skipped.append((ins.wallet_address, ins.amount_mojos,
                            "already sent in this cycle"))
            store.mark_skipped(plan.cycle_id, ins.wallet_address,
                               "already sent in this cycle")
            continue

        attempt, last_err = 0, ""
        while attempt < max_attempts:
            attempt += 1
            # Committed BEFORE the RPC: a crash between here and the response
            # must leave evidence that a spend may be in flight.
            store.mark_sending(plan.cycle_id, ins.wallet_address)
            try:
                tx_id = spender.send(ins)
            except ExecutorError as e:
                # The wallet responded but we cannot tell what it did. Never
                # retried — this is the double-spend case.
                store.event("unknown-outcome", cycle_id=plan.cycle_id,
                            wallet=ins.wallet_address, error=str(e))
                halted = (
                    f"UNKNOWN OUTCOME for {ins.wallet_address}: {e} "
                    f"The instruction is left in `sending`. Check the wallet for "
                    f"a transaction of {ins.amount_mojos} mojos before re-running; "
                    f"no further cycle will start until this is resolved.")
                break
            except Exception as e:      # noqa: BLE001 - transport/daemon faults
                last_err = f"{type(e).__name__}: {e}"
                store.event("send-failed", cycle_id=plan.cycle_id,
                            wallet=ins.wallet_address, attempt=attempt,
                            error=last_err)
                if attempt < max_attempts:
                    sleep(backoff_seconds * (2 ** (attempt - 1)))
                continue
            else:
                store.mark_sent(plan.cycle_id, ins.wallet_address, tx_id)
                sent.append((ins.wallet_address, ins.amount_mojos, tx_id))
                store.event("sent", cycle_id=plan.cycle_id,
                            wallet=ins.wallet_address,
                            mojos=ins.amount_mojos, tx_id=tx_id)
                break
        else:
            store.mark_failed(plan.cycle_id, ins.wallet_address, last_err)
            failed.append((ins.wallet_address, ins.amount_mojos, last_err))

        if halted:
            break

    store.set_cycle_state(plan.cycle_id, "halted" if halted else "settled")
    return ExecutionReport(cycle_id=plan.cycle_id, dry_run=False,
                           sent=tuple(sent), failed=tuple(failed),
                           skipped=tuple(skipped), halted_reason=halted)


def track_confirmations(cycle_id: str, *, store: audit_mod.AuditStore,
                        spender: WalletSpender) -> dict[str, bool]:
    """Ask the wallet whether each sent transaction has confirmed.

    Separate from ``execute`` on purpose: confirmation takes minutes and a
    payout run should not hold a process open waiting for the chain.
    """
    out: dict[str, bool] = {}
    for row in store.instructions(cycle_id):
        if row.state != audit_mod.SENT or not row.tx_id:
            continue
        ok = spender.confirmed(row.tx_id)
        out[row.wallet_address] = ok
        if ok and not row.confirmed:
            store.mark_confirmed(cycle_id, row.wallet_address)
            store.event("confirmed", cycle_id=cycle_id,
                        wallet=row.wallet_address, tx_id=row.tx_id)
    return out
