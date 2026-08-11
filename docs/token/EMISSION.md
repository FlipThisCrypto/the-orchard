# $JUICE Emission Model — canonical specification

**Status:** Active. **Model version:** `2.0.0`.
**Implementation:** [`orchard_chia/economics/`](../../orchard_chia/economics/) —
the only place economic constants live. Nothing else may hardcode a rate, cap
or share.

**Operator surface:** `python -m orchard_chia.economics status | report |
settle --season N | pay` — see the
[operator runbook](../ops/DATALAYER_OPERATOR.md). Settlement records owed
amounts in an append-only pool ledger; `pay` turns settled days into spends
through the dry-by-default planner/executor stack.

This document and that package are the single source of truth. Where any other
document disagrees, it is out of date.

---

## What the token is for

The Orchard exists to build an open network of real environmental sensors and
verifiable telemetry. $JUICE pays the people running that infrastructure. It
supports the ecosystem rather than being it.

---

## Fixed supply

| | JUICE | mojos |
|---|---:|---:|
| Total supply | 100,000,000 | 100,000,000,000 |
| Tree Rewards Pool (85%) | 85,000,000 | 85,000,000,000 |
| Liquidity (15%) | 15,000,000 | 15,000,000,000 |
| Founder / team | **0** | **0** |

No JUICE is ever minted beyond the total. There is no founder allocation from
the fixed supply; the constant exists and is zero, so introducing one would be
a visible change to a governed file rather than an addition nobody has to
justify.

$JUICE is a 3-decimal Chia CAT: **1 JUICE = 1,000 mojos**, and 0.001 JUICE is
the smallest representable amount.

---

## The core rule

**The network has a maximum daily emission. Trees divide it. More Trees can
never raise it.**

This is the whole early-adopter incentive, and it needs no separate multiplier
to produce:

| Eligible Trees | Potential per Tree, year 1 |
|---:|---:|
| 1 | 55,964.65 |
| 10 | 5,596.47 |
| 100 | 559.65 |
| 1,000 | 55.96 |
| 10,000 | 5.60 |

Actual reward is that figure reduced by the Tree's own uptime.

---

## Eight-year base schedule

Maximum **daily network** emission, each year ~20% below the last:

| Year | JUICE/day | mojos/day |
|---:|---:|---:|
| 1 | 55,964.65 | 55,964,650 |
| 2 | 44,771.72 | 44,771,720 |
| 3 | 35,817.38 | 35,817,380 |
| 4 | 28,653.90 | 28,653,900 |
| 5 | 22,923.12 | 22,923,120 |
| 6 | 18,338.50 | 18,338,500 |
| 7 | 14,670.80 | 14,670,800 |
| 8 | 11,736.64 | 11,736,640 |

Rates are **stated exactly**, not derived by compounding 0.8. Repeated
floating-point multiplication does not reproduce these figures, and a schedule
that drifts by a mojo a year is a schedule two implementations will eventually
disagree about.

Over 8 × 365 days this totals ~85,000,000 JUICE — which is what makes eight
years the intended runway *if essentially every scheduled reward is earned*.

### After year 8

The year-8 ceiling — **11,736.64 JUICE/day** — continues until the pool reaches
zero. The schedule is a floor on the programme's life, not an expiry.

---

## Unearned rewards extend the runway

**JUICE not earned because of missed uptime stays in the Tree Rewards Pool.**

It is not burned, not redistributed to Trees that were up, not swept to
liquidity or treasury, and not counted as emitted.

> Ceiling 10,000 · earned 7,500 → 7,500 distributed, **2,500 stays in the pool**.

Measured from the implementation, four Trees at 75% uptime:

- pool depletes on **day 5,340 — 14.6 years**, against an 8-year schedule.

Public wording:

> The Orchard has a minimum eight-year Tree reward emission schedule. Rewards
> not earned because of missed uptime remain in the reward pool and extend the
> lifetime of Tree rewards beyond the original eight-year schedule.

---

## Heartbeats

24 reward windows per day, one per hour.

```
uptime_factor = verified_heartbeats / 24
```

24/24 = 100% · 18/24 = 75% · 12/24 = 50% · 0/24 = 0%.

Only heartbeats that actually verify count. More than 24 in a day is refused,
so a burst cannot buy more than a day of uptime.

---

## Sensor weighting

| Qualifying sensors | Weight |
|---:|---:|
| 1 | 1.00 |
| 2 | 1.05 |
| 3 | 1.10 |
| 4 | 1.15 |
| 5 | 1.20 |
| 6+ | 1.25 (cap) |

Weights are exact twentieths (`21/20`, not `1.05`). They change **how the fixed
pool is divided** and can never enlarge it, which is why the cap can be
generous without any supply risk.

A Tree needs **at least one qualifying sensor**. A board sending only
heartbeats earns nothing — and contributes nothing to the denominator, so it
cannot dilute real Trees while earning zero itself.

Sensor **classes** rather than raw count is the intended direction: temperature,
humidity, pressure, particulates, gas, CO₂, light/UV, rain, soil, noise,
geophysical, power. The qualifying-sensor count is a single configurable input,
so that evolution changes one function.

---

## The canonical formula

```
sensor_weight  = min(1 + 0.05 × (qualifying_sensors − 1), 1.25)
total_weight   = Σ sensor_weight over eligible Trees
potential      = daily_ceiling × sensor_weight / total_weight
uptime_factor  = verified_heartbeats / 24
tree_reward    = floor(potential × uptime_factor)      # mojos

distributed    = Σ tree_reward
unearned       = daily_ceiling − distributed           # stays in the pool
```

**Rewards belong to Trees, not wallets.** A wallet owning three Trees is three
participants. Wallet totals are summed only at settlement and are never an
input, which is what makes splitting or merging wallets pointless.

### Precision

Exact rationals throughout, one `floor` per Tree, integer mojos out. Every
Tree rounds **down** and the remainder stays in the pool — that is the model's
central rule expressed in arithmetic, not a rounding convenience. Consequences:

- `Σ tree_reward ≤ daily_ceiling`, always
- `Σ all rewards ever ≤ 85,000,000 JUICE`, always
- identical output for any input ordering, on any machine

---

## Enforced invariants

1. Total supply 100,000,000 — never exceeded
2. Tree Rewards Pool 85,000,000 — never over-distributed
3. Liquidity 15,000,000 — accounted separately
4. More Trees never raise daily emission
5. Unearned rewards stay in the pool
6. Sensor bonuses never inflate emission
7. Wallet count cannot affect allocation
8. 24 heartbeats = 100% uptime
9. Forfeited rewards are never redistributed
10. Rewards stop at zero; the pool never goes negative

Each is pinned by a named test in
[`orchard_chia/tests/test_economics.py`](../../orchard_chia/tests/test_economics.py).

---

## Eligibility

Decided upstream, not re-derived by the reward maths. A Tree must be
registered, wallet-owned, hold a valid Pass where applicable, carry ≥1 approved
sensor, present valid device signatures, pass replay/sequence protection, and
be free of duplicate identity or known spoofing.

An ineligible Tree is **recorded with its reason**, not silently dropped, and
does not contribute to `total_weight`.

---

## Decisions since ratification

- **Genesis day — RESOLVED.** The emission calendar reuses the season
  calendar: `day_index = season − 1`, genesis `ORCHARD_SEASON_GENESIS`
  (2026-05-27). One calendar, one boundary; no drift between the day rewards
  think it is and the day uptime was counted against.
- **Sensor classes — IMPLEMENTED.** `oracle/app/sensor_classes.py` holds the
  approved class map (a governed dict), multi-measurement devices credit each
  class once, redundant same-class devices credit once, qualification requires
  ≥12 reporting hours and physically plausible values
  (`PLAUSIBLE_RANGES`). Extending the list is a data change, not logic.
- **Chain-first settlement — DEFAULT.** Where a signed on-chain seal exists
  for a season it outranks the oracle's own count; a failed consult REFUSES
  rather than falling back (the chain's figure is never higher, so falling
  back can only overpay, and a day settles once). `ORCHARD_SETTLE_CHAIN=0`
  opts out deliberately, e.g. on a host with no DataLayer daemon.
- **Sensor persistence is relative.** A sensor qualifies by reporting through
  half the hours the Tree was CREDITED online, capped at 12 (half a full day —
  what the old flat 12 always meant). A flat bar made partial uptime
  unearnable rather than proportionally paid.
- **Heartbeat integrity — IMPLEMENTED.** An hour credits only with ≥30
  accepted readings spread across ≥4 ten-minute slots; replay protection is
  on by default; a sealed on-chain season outranks the oracle's own count
  (`ORCHARD_SETTLE_CHAIN=1`).

## Open decisions requiring owner input

1. **Terminal dust.** Sustained sub-100% uptime strands a few mojos the pool
   cannot pay out until uptime is perfect. They remain earnable — leave them,
   or sweep once to liquidity?
2. **Pass requirement.** Whether an Orchard Pass is mandatory for rewards, or
   only for claiming a Tree.
3. **The 188 existing attestations.** Scored under the superseded 1 JUICE/Tree/day
   model and unpayable under it. Nothing has been paid, so nothing needs
   unwinding — but a public statement of that may be wanted.
4. **Payout wallet address.** The public receive address for reward outflows
   (docs/token/JUICE.md still carries the TODO).

---

## Superseded models

Kept in the tree, marked, because records they produced are on chain and a
reader must be able to reconstruct what was computed at the time.

| Module | Model | Why superseded |
|---|---|---|
| `orchard_chia/payout/` | 1 JUICE per Tree per day | No ceiling at all — 10,000 Trees would mint 10,000/day |
| `orchard_chia/allocation/` | Fixed budget ÷ each wallet's mean Tree uptime | Weighted wallets, so a second Tree could *reduce* an operator's share |

This model is the reconciliation: a fixed **network** ceiling that more Trees
can only divide, with weight attaching to **Trees**.
