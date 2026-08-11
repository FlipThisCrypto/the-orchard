# $JUICE — Token Reference (Public)

> Public-safe information about the $JUICE CAT. The original creation log (`docs/Juice Token.docx`) contains operator-private wallet info and is **excluded from the repo by `.gitignore`** — never commit it.

![$JUICE logo](../photos/logo.png)

## Identity

| Field          | Value                                                                |
|----------------|----------------------------------------------------------------------|
| Name           | $JUICE                                                               |
| Type           | CAT (Chia Asset Token)                                               |
| Blockchain     | Chia mainnet                                                         |
| Asset ID       | `285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121`   |
| Eve Coin ID    | `2ff338ed6fb3161d48eed7f112d3c6077e90c517dc4534bfba8ad3975b7f5e63`   |
| Issuance       | Single issuance                                                      |
| Total Supply   | 100,000,000 JUICE                                                    |
| Project        | The Orchard                                                          |

## Verifying the token

Anyone can verify the $JUICE token on-chain by querying for the Asset ID or Eve Coin ID through:

- The Chia reference wallet (`chia wallet show` after adding the CAT)
- A Chia block explorer (e.g., [SpaceScan](https://www.spacescan.io/), [Mintgarden](https://mintgarden.io/))
- A full node RPC call against the Asset ID

## Description

JUICE is the native reward token of **The Orchard** — an open-source DePIN ecosystem on the Chia blockchain focused on decentralized sensor networks, real-world telemetry, environmental data, and community-built infrastructure.

The token is hardware-first and infrastructure-first: it pays operators for running real-world sensing Trees, not for speculative behavior. See [`../VISION.md`](../VISION.md) for the design philosophy.

## How $JUICE is distributed

See [reward economics in the README](../../README.md#reward-model-v1-tunable) and the manual-payout flow in [`../../orchard_chia/README.md`](../../orchard_chia/README.md). v1 distribution is a manual Season harvest from the issuer wallet; future versions may move to on-chain claim flows (epoch vaults, [ADR-0008](../decisions/0008-serverless-target-architecture.md)).

## Treasury & allocation

Total supply is **100,000,000 JUICE** (single issuance). The allocation below is
the published breakdown of where that supply is committed.

Ratified 2026-08-10. Canonical figures live in
[`orchard_chia/economics/constants.py`](../../orchard_chia/economics/constants.py);
the full model is [EMISSION.md](EMISSION.md).

| Allocation        | Share | Amount (JUICE) | Purpose                                        |
|-------------------|-------|----------------|------------------------------------------------|
| Tree Rewards Pool | 85%   | 85,000,000     | Operator rewards (the core sink)               |
| Liquidity         | 15%   | 15,000,000     | DEX / market liquidity                         |
| Team / founder    | 0%    | 0              | No allocation is carved from the fixed supply  |
| **Total**         | 100%  | 100,000,000    |                                                |

### Payout wallet

The Season-harvest payouts are sent from a dedicated, thinly-funded payout key
(see the deploy runbook's wallet-hygiene section). The **public receive
address** for that wallet, once the owner chooses to publish it, goes here so
the community can audit reward outflows on-chain:

> **`TODO(owner)`** — publish the payout wallet's `xch1…` receive address (a
> receive address is public-safe; never publish the fingerprint, wallet id, or
> mnemonic — those stay gitignored per the section below).

## Emission expectations

**See [EMISSION.md](EMISSION.md) for the canonical model.** In short: the whole
network has a fixed maximum daily emission, and Trees divide it. Adding Trees
never raises total emission — it divides the same pool further, which is the
entire early-adopter incentive.

Year 1 ceiling is **55,964.65 JUICE/day** for the network, falling ~20% a year
across an eight-year base schedule. Rewards not earned through downtime stay in
the pool and extend the programme beyond eight years.

<details>
<summary><b>Superseded:</b> the v1 per-Tree rate (historical record)</summary>

Until 2026-08-10 the model was:

- **1 JUICE per Tree per day**, accrued **1/24 JUICE per verified uptime hour**.

It had no emission ceiling of any kind — total supply scaled with fleet size,
so 10,000 Trees would have minted 10,000 JUICE/day and exhausted the entire
100,000,000 supply in under 28 years of growth it was supposed to encourage.
Every attestation currently on the DataLayer store was scored under it. Nothing
was ever paid out under it.

Retained because on-chain records reference it and a reader needs to be able to
reconstruct what was computed at the time.

</details>

<details>
<summary>Fleet-size table under the superseded model</summary>


| Fleet size | Max JUICE / Season (all Trees 100% uptime) |
|------------|---------------------------------------------|
| 10 Trees   | ~10                                          |
| 100 Trees  | ~100                                         |
| 1,000 Trees| ~1,000                                       |

</details>

That open question — at what fleet size or date does the rate step down, and how
long does the pool last — is what the 2026-08-10 model answers. The rate steps
down annually rather than by fleet size, the pool funds a minimum of eight
years, and unearned rewards extend it. See [EMISSION.md](EMISSION.md).

## Legal note

This document describes token mechanics for transparency; it is **not legal or
financial advice**. Token issuance, rewards, and any sale or distribution
mechanics should get **qualified legal review** (securities, tax, consumer
protection in the relevant jurisdictions) **before broad public launch**.
Treat the numbers above as engineering parameters, not commitments, until that
review and the owner's sign-off are complete.

## What's *not* in this file

The operator's wallet fingerprint, wallet id, and wallet label are intentionally **not** documented here. They live in:

- The local `orchard_chia/config.yaml` (gitignored)
- The local `docs/Juice Token.docx` (gitignored)
- Memory at `~/.claude/.../memory/project_token_juice_private.md` (local only, not in this repo)

Operators forking this project will create their **own** CAT and substitute their own asset id. This file is the template.
