# ADR-0001: v1 Architecture

- **Status:** Accepted
- **Date:** 2026-05-27
- **Deciders:** Richard Aubrey (FlipThisCrypto)

## Context

The Orchard is a proof-of-concept open-source DePIN on the Chia blockchain. ESP32-based environmental sensing nodes will collect verifiable data, have their uptime attested on-chain, and earn a Chia CAT token. The project is meant to be approachable for novices to fork and extend.

Before writing any code we needed to lock in the architectural shape so we don't accidentally build the wrong thing.

## Decisions

### 1. Dashboard delivery: local Python web app

A Flask app the user runs on their PC (`python -m dashboard.app`) and opens in any browser at `http://localhost:5000`.

**Why:** One-command install. No platform-specific builds. Trivial for novices to replicate. Easy for contributors to hack on without a build pipeline.

**Rejected alternatives:**
- Electron/Tauri desktop app — adds build complexity and ~80 MB per release.
- Cloud-hosted web app — premature for PoC; requires hosting, auth, DB infra.

### 2. Chia network: mainnet only

Richard's CAT token is already minted on mainnet and he has tokens in-wallet. Tests will use small mojo amounts.

**Why:** The CAT already exists. Doubling the work to maintain a parallel testnet config buys little for a small node count.

### 3. Chia infrastructure: local full node + DataLayer

Both run on the same Windows PC as the dashboard. The dashboard and oracle hit Chia RPCs on localhost.

**Why:** Simplest networking. No TLS or auth gymnastics. Sensitive credentials stay on the device.

### 4. Firmware: rewrite from scratch in PlatformIO + Arduino framework

The previously-running ESP32 firmware (and its repurposed oracle backend) returned 500s due to schema mismatches between the old codebase and the new node design. v1 firmware will be modular: each sensor type is a self-registering driver, each radio (WiFi today, LoRa/cellular future) is behind an interface.

**Why:** A misaligned reuse cost us 500 errors and a BH1750 false alarm. A clean modular base costs one upfront rewrite and saves dozens of patches later. Modular interfaces let future contributors swap radios and power modules without forking the whole firmware.

### 5. Reward distribution: manual batched CAT spend bundle

Oracle tracks uptime per wallet. Once per Season a script builds a single $JUICE CAT spend bundle paying every eligible Tree operator. Richard reviews and signs through the official Chia wallet RPC.

**Why:** No Chialisp claim contract to audit. Simple, debuggable, swappable. We can upgrade to a trustless on-chain claim flow later without disrupting the data pipeline.

**Rejected alternatives:**
- On-chain claim contract — weeks of Chialisp work, audit surface, premature for PoC.
- Hybrid signed-attestation pull-claim — still requires Chialisp, no decisive advantage at this scale.

### 6. NFT credential: design and mint a fresh CHIP-7 collection — **Orchard Passes**

One Orchard Pass per registered Tree operator. v1 enforces 1 Orchard Pass per wallet at registration time (oracle-side check). Ownership is verified by querying the wallet RPC for the collection's launcher id. (Per the vision: future Orchard Passes get tiers and reward multipliers — out of scope for v1.)

**Why:** Lightweight identity layer. Cryptographic proof of node ownership. Easy to upgrade to richer credential schemes later (verifiable attestations, on-chain reputation).

### 7. DataLayer scope: daily uptime attestations only

One signed record per node per Season: `{node_id, Season, hours_online, data_hash}`. Raw sensor readings stay in the oracle's local SQLite database.

**Why:** Keeps DataLayer write costs predictable. Provides cryptographic proof of uptime — sufficient for trustless reward calculation. Raw data can be made verifiable later via a Merkle commitment if/when needed.

### 8. Repo: public GitHub from day one, Apache 2.0

Public at https://github.com/FlipThisCrypto/the-orchard (originally created as `DeMeterData`, renamed 2026-05-27 — GitHub redirects the old URL). Apache 2.0 license.

**Why:** Maximum community signal. Patent grant in Apache 2.0 protects contributors and integrators of this infrastructure project.

## Reward economics (v1, all tunable via config)

> **SUPERSEDED 2026-08-10** by the fixed-supply emission model in
> [docs/token/EMISSION.md](../token/EMISSION.md). The rate below had no network
> emission ceiling: total supply scaled with fleet size. Left unedited below as
> the historical record — every attestation on the DataLayer store was scored
> under it, and a reader must be able to reconstruct what was computed at the
> time. Nothing was ever paid out under it.

| Parameter      | Value                                       |
|----------------|---------------------------------------------|
| Daily rate     | 1 $JUICE per Tree per day                   |
| Accrual unit   | 1/24 $JUICE per verified hour               |
| Season length  | 4608 Chia blocks (~24h)                     |
| Credential cap | 1 Orchard Pass per wallet                   |

These are intentionally conservative test-phase numbers. The codebase exposes them as configuration, not constants — the rate can be scaled up after initial testing without redeploying the stack.

## Deployment horizon

- **v1 (now):** 1–5 mains-powered WiFi Trees within Richard's reach. Always-on power, home WiFi, USB serial for setup.
- **v1.x:** Tens of Trees regionally (the first Groves). Adds: setup wizard, OTA, remote diagnostics, varied WiFi conditions.
- **v2+:** Hundreds of off-grid Trees, Keepers validating submissions. Adds: LoRa or cellular fallback, battery + solar power budgeting, possibly Chialisp claim contract. See [VISION.md](../VISION.md) for the full picture.

Firmware modularity (sensors, radios, power management behind interfaces) is non-negotiable from day one to keep that path open.

## Consequences

- We will not implement Chialisp smart-coin payouts in v1. Anyone relying on trustless on-chain claims will be disappointed until v2.
- The oracle is a centralized trust point in v1. Acceptable for PoC; documented as a known limitation.
- Dashboard runs locally, so it does not support remote monitoring of nodes deployed elsewhere. Documented limitation; v1.x scope.
- All sensor drivers must conform to the modular interface. New contributors will be pointed at the [examples/sensor_driver/](../../examples/sensor_driver/) template.

## Open questions (carried over)

- ~~CAT Asset ID — needed at Phase 7.~~ **Resolved 2026-05-27:** `285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121` ($JUICE).
- ~~Second antenna purpose~~ **Resolved 2026-05-27:** WiFi/Bluetooth + corded GPS. No LoRa.
- Final domain / hosted Orchard View plan — out of scope for v1.

## See also

- [VISION.md](../VISION.md) — long-term direction. Most items there are explicitly *not* v1 scope. This ADR is the authoritative v1 boundary.
