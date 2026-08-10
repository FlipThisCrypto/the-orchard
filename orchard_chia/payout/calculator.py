# SPDX-License-Identifier: Apache-2.0
"""Reward calculation — pure functions, no I/O.

The math is intentionally trivial in v1: ``tokens = (hours_online / 24) * daily_rate``.
Future versions will add multipliers (Pass tier, sensor diversity,
geographic scarcity, validated submissions, reputation) — they'll
slot in as additional arguments to the same function.

CAT amounts are returned in **mojos** (the on-chain integer unit).
$JUICE is a CAT with 3 decimals on Chia, so:

    1 $JUICE  = 1000 mojos
    0.1       =  100 mojos
    0.01      =   10 mojos
    0.001     =    1 mojo

The smallest unit is 0.001 $JUICE. Sub-mojo rewards round toward zero.
"""
from __future__ import annotations

CAT_MOJOS_PER_TOKEN = 1000


def _unproven(attestation: dict, why: str, *, pay_unproven: bool) -> tuple[int, str]:
    """What an unprovable record is worth.

    Zero, unless the caller explicitly opts out.

    This used to fall back to the operator's own ``hours_online`` claim, on the
    reasoning that paying 0 would be a unilateral reward-policy change. That was
    a fair call when it was written. It is no longer the policy: "if the answer
    to a placeholder is 0, then 0 is given — 0 hours uptime is 0 hours uptime
    meaning 0% payout of $JUICE".

    The scale of what the old rule permitted, measured against the live store on
    2026-08-10: all 188 attestations paid, 170.033 $JUICE, every one of them on
    ``hours_online (unverified)``. 184 of those records belong to Trees that
    have since been retired as duplicates, and 3 to a node_id that exists only
    in this repo's test fixtures. Not one had a single published reading behind
    it. The fallback did not pay a little too much; it paid the entire ledger
    for evidence that did not exist.

    ``pay_unproven=True`` restores the old amounts for anyone reconciling
    historical figures. It is off by default and must be asked for by name.
    """
    if not pay_unproven:
        return 0, f"unproven ({why})"
    try:
        claim = int(attestation.get("hours_online") or 0)
    except (TypeError, ValueError):
        claim = 0
    return claim, f"hours_online (unverified, {why})"


def paid_hours(attestation: dict, *, prefer_verified: bool = True,
               pay_unproven: bool = False) -> tuple[int, str]:
    """The hours a reward is computed on, plus which field they came from.

    Pays on the verifiable ``verified_hours`` (SPEC §3/§11) — the number anyone
    can recompute from the public ``readings:`` rows. Returned with its basis so
    the payout report shows what the money actually rests on.

    **Nothing unprovable is paid.** A record that declares
    ``seal_source == "placeholder"``, or carries no ``verified_hours`` at all,
    or declares no verification basis, has nothing behind it that a third party
    could check. Those pay zero. The oracle's ``hours_online`` is its own
    account of itself; it is reported, never paid on.

    This reverses an earlier decision to fall back to ``hours_online`` for
    placeholders — see :func:`_unproven` for what that fallback cost when
    measured against the real store.

    **Fail closed, always toward the smaller number.** An unrecognized basis or
    a future schema value keeps paying ``verified_hours`` rather than routing to
    the larger claim; a placeholder that contradicts itself pays nothing at all.
    No path here can make a record worth MORE by being less verifiable.

    The payer and the verifier must not disagree about the same signed bytes, so
    this consults :func:`schema.attest_basis` rather than re-deriving honesty
    rules — every case the verifier can't call proven is labelled here too.
    """
    from ..datalayer import schema as dl_schema

    def _int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    vh = attestation.get("verified_hours")
    if not prefer_verified:
        return _int(attestation.get("hours_online")), "hours_online"

    if vh is None:
        # No verified_hours field at all. Nothing here can be recomputed from
        # public readings, so there is nothing to pay ON — only the oracle's
        # word for it. See _unproven() for why that is now worth zero.
        return _unproven(attestation, "no verified_hours field",
                         pay_unproven=pay_unproven)

    vh = _int(vh)

    if dl_schema.attest_declares_placeholder(attestation):
        if dl_schema.placeholder_inconsistency(attestation):
            # Self-contradictory: declares "nothing published" while claiming a
            # verified number. The verifier calls this a definitive defect, so
            # the payer must not quietly pay it. No honest writer emits it.
            return 0, "unpayable (inconsistent attestation)"
        return _unproven(attestation, "placeholder", pay_unproven=pay_unproven)

    basis, _why = dl_schema.attest_basis(attestation)
    if basis is True:
        return vh, "verified_hours"
    if basis is None:
        # No basis declared, but a verified number IS present. That is a
        # pre-1.1.0 record, not a placeholder — someone computed this from
        # readings even if the record cannot say so. Paying it is fail-closed
        # already: verified_hours is the smaller, provable number, never the
        # operator's claim. The label carries the caveat so the report does not
        # print a bare "verified_hours" for something a third-party verifier can
        # only call unproven.
        return vh, "verified_hours (basis undeclared)"

    # Declared "readings" but not actually signature-verified, or an
    # unrecognized basis. Amount is unchanged (never inflate to the claim);
    # only the label changes, so the report can show what it rests on.
    if str(attestation.get("seal_source")) == dl_schema.SEAL_SOURCE_READINGS:
        return vh, "verified_hours (sigs unchecked)"
    return vh, "verified_hours (basis unrecognized)"


def juice_mojos_for_attestation(
    attestation: dict,
    *,
    daily_rate: float,
    prefer_verified: bool = True,
    pay_unproven: bool = False,
) -> int:
    """Reward (in $JUICE mojos) for a single signed attestation.

    Pays on the **verifiable** metric: ``verified_hours`` — the hours anyone can
    recompute from the public ``readings:`` rows (SPEC §3/§11) — when the
    attestation carries it (secp256r1 attests do), falling back to the oracle's
    ``hours_online`` claim for older records. Paying on ``verified_hours`` means
    an oracle *over-count is not rewarded*: even payouts obey the tenet. When the
    oracle is honest the two numbers are equal and the reward is unchanged.

    Caller is responsible for verifying the attestation's signature before
    passing it in — this function trusts the contents.
    """
    hours, basis = paid_hours(attestation, prefer_verified=prefer_verified,
                              pay_unproven=pay_unproven)
    if hours < 0 or hours > 24:
        raise ValueError(f"{basis} out of range: {hours}")
    if daily_rate < 0:
        raise ValueError(f"daily_rate must be >= 0, got {daily_rate}")
    juice = (hours / 24.0) * float(daily_rate)
    return int(round(juice * CAT_MOJOS_PER_TOKEN))


def mojos_to_juice(mojos: int) -> float:
    """Format helper for human-readable display."""
    return mojos / CAT_MOJOS_PER_TOKEN


def aggregate_by_wallet(
    rewards: list[dict],
) -> dict[str, int]:
    """Sum mojos owed per recipient wallet.

    ``rewards`` is a list of ``{"wallet_address": str, "mojos": int}``.
    Returns ``{wallet_address: total_mojos}``. Skips entries with
    falsy or empty wallet_address (those Trees haven't bound a wallet
    yet — operator hasn't completed registration).
    """
    out: dict[str, int] = {}
    for r in rewards:
        addr = r.get("wallet_address") or ""
        if not addr:
            continue
        out[addr] = out.get(addr, 0) + int(r.get("mojos", 0))
    return out
