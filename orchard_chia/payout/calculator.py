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
    """
    vh = attestation.get("verified_hours")
    if prefer_verified and vh is not None:
        return int(vh), "verified_hours"
    return int(attestation.get("hours_online", 0)), "hours_online"


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
