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


def paid_hours(attestation: dict, *, prefer_verified: bool = True) -> tuple[int, str]:
    """The hours a reward is computed on, plus which field they came from.

    Prefers the verifiable ``verified_hours`` (SPEC §3/§11) when present, else
    the oracle's ``hours_online`` claim. Returned so the payout report can show
    the basis actually paid on rather than a claim that wasn't.

    **Schema 1.1.0 basis awareness.** A record that explicitly declares
    ``seal_source == "placeholder"`` was written with nothing published on
    chain, so its ``verified_hours`` is 0 by construction — *nothing was
    verified*. Paying 0 there would be a unilateral reward-policy change, so we
    fall back to the operator's ``hours_online`` claim, which is exactly the
    amount this path has always paid. The difference is that the basis is now
    ``hours_online (unverified)`` and the report says so, instead of a
    self-report masquerading as the verified metric.

    A record with no ``seal_source`` (pre-1.1.0) keeps the previous behavior.

    **The fallback is deliberately narrow.** It fires ONLY for a record that is
    *consistently* a placeholder — it declares ``seal_source == "placeholder"``
    **and** carries ``verified_hours == 0``, which is exactly what this repo's
    writer produces. Anything else (an unrecognized basis, a future 1.2 value, a
    placeholder claiming non-zero verified hours) keeps paying ``verified_hours``.
    A broader rule would let a non-``"readings"`` string route payment to the
    larger unverified claim and **inflate** the reward — the precise inversion of
    "an over-count is not rewarded". Fail closed toward the smaller, provable
    number; never toward the operator's claim.

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
    if not prefer_verified or vh is None:
        return _int(attestation.get("hours_online")), "hours_online"

    vh = _int(vh)
    hours_claim = _int(attestation.get("hours_online"))

    if dl_schema.attest_declares_placeholder(attestation):
        if dl_schema.placeholder_inconsistency(attestation):
            # Self-contradictory: declares "nothing published" while claiming a
            # verified number. The verifier calls this a definitive defect, so
            # the payer must not quietly pay it. No honest writer emits it.
            return 0, "unpayable (inconsistent attestation)"
        # Consistent placeholder: verified_hours=0 means "unproven", not "zero
        # uptime". Pay the claim — identical to the pre-1.1.0 amount — but say so.
        return hours_claim, "hours_online (unverified)"

    basis, _why = dl_schema.attest_basis(attestation)
    if basis is True:
        return vh, "verified_hours"
    if basis is None:
        # No basis declared. Amount is the legacy one (unchanged), but the payer
        # must not print a bare "verified_hours" for a record a third-party
        # verifier can only call unproven — that divergence is how a stripped
        # basis reads as verified to the one party that pays.
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
    hours, basis = paid_hours(attestation, prefer_verified=prefer_verified)
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
