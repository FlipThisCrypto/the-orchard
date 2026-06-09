# ADR-0003: DataLayer as a publicly verifiable environmental dataset

- **Status:** Accepted
- **Date:** 2026-06-09
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Supersedes:** ADR-0001 §7 ("DataLayer scope: daily uptime attestations only")
- **Related:** ADR-0002 (Grove federation), [`docs/datalayer/SPEC.md`](../datalayer/SPEC.md) (concrete wire format)

## Context

ADR-0001 §7 scoped Chia DataLayer to **one signed uptime record per node per
Season** — `{node_id, season, hours_online, data_hash}` — with the raw sensor
readings staying in the oracle's local SQLite, and an explicit note that "raw
data can be made verifiable later via a Merkle commitment if/when needed."

That moment has arrived. Two structural limits in the current pipeline block
the project's larger goal — a public, trustworthy environmental record:

1. **The signature is symmetric.** Attestations are signed with `HMAC-SHA256`
   using the oracle's secret key (`orchard_chia/datalayer/attest.py`).
   `verify_signature()` needs that *same secret* to check it. So **no outside
   party can verify anything** — only the operator who holds the key. For a
   public dataset this is fatal.
2. **The data isn't on chain.** Only `hours_online` reaches DataLayer. The
   actual air-quality / temperature / GPS values — the valuable part — live in
   a local DB that disappears with the operator's PC. `data_hash` is a
   placeholder (`sha256(node:season:hours)`) that commits to the *summary*,
   not the readings.

We want The Orchard to be the verifiable, permanent, decentralized
environmental record that centralized APIs (government feeds, PurpleAir, etc.)
structurally cannot be: data anyone can prove is **on chain, unchanged, and
produced by a real device**.

Chia gives us most of that stack natively, for free:

| Guarantee | Question | Source |
|---|---|---|
| Permanence + integrity | "On chain & unchanged since block N?" | DataLayer store = singleton with an on-chain Merkle root, updated every `batch_update`; `get_proof` yields inclusion proofs. |
| Operator authenticity | "Whose store is this?" | The store singleton is owned by the operator's wallet. |
| **Device provenance** | "Did a real device measure this, or did the operator type it in?" | *Nothing native — this is the one piece we must add.* |

The whole "verifiable" claim collapses on that third row. Everything below
follows from closing it.

## Decisions

### 1. Device-signed readings with asymmetric keys (ed25519)

Each Tree generates an **ed25519 keypair on first boot** (stored in NVS beside
`node_id`), signs every reading, and exposes its **public key** over serial
(`HW_INFO`) and at registration. The pubkey is published in the Tree's
DataLayer `node:` record. Anyone can then verify a reading was produced by that
device's key. This replaces the HMAC device→oracle scheme.

- **ed25519, not BLS,** for the device: ~milliseconds per signature on an
  ESP32-S3 vs. hundreds of ms for BLS12-381, with broad library support
  (`libsodium` / `micro-ecc`). BLS is held in reserve for the *optional* Season
  signature, where Chia-native verification could help.
- The key never leaves the device. node_id + key survive reflash (no NVS erase).

### 2. Publish the full readings — batched and Merkle-rooted

The real environmental readings go to DataLayer in **hourly batches**. Each
Season's `attest:` record carries a **Merkle root over that Season's readings**
(replacing the placeholder `data_hash`). **This supersedes ADR-0001 §7.**

Powerful consequence: **uptime becomes independently recomputable from public
data.** A Keeper fetches the readings, verifies each device signature, and
recomputes `hours_online` and the root themselves — *no trust in the oracle
required*. The oracle stops being an authority and becomes a convenience.

### 3. Anti-backdating via block-hash anchoring

Each signed reading embeds a **recent Chia block-hash prefix**. You can't sign a
block hash that doesn't exist yet, so this bounds a reading's creation time from
below — a cheap defense against fabricated history. (Anchor source: oracle
`/beacon` in v1; direct RPC in v1.x — see SPEC, with the trust nuance noted.)

### 4. Self-describing store schema

A `meta:schema` record describes version, units, geohash precision, and signer
scheme, so a **generic viewer renders any operator's store** with no prior
knowledge. This is what makes the public dashboard and the open-standard
ambition possible.

### 5. Two-tier publish cadence

- **Hot path:** an hourly `batch_update` writing the just-closed hour's
  `readings:` batch (~24 tx/day/operator). Cadence is a config knob.
- **Sealed:** the per-Season `attest:` record (Season root + uptime + oracle
  signature) at Season close (~4608 blocks / ~24h).

### 6. Public dashboard — "Orchard Atlas" (reads DataLayer, not the oracle)

A new **read-only public app** that reads DataLayer (via `chia data subscribe`
/ an indexer), independent of any single oracle. Global map, per-Tree timeline,
per-reading **Verify**. The existing local **Orchard View** stays the
operator's control panel; the two are now distinct surfaces.

### 7. Privacy model — coarse-by-default location

- **Public:** environmental values, **coarse geohash (default precision 5 ≈
  5 km)**, pseudonymous `node_id`, device pubkey, uptime, roots, signatures.
- **Private / opt-in:** precise GPS, wallet address, network info, all secrets.
  Precise location is never the default (home-location safety).

### 8. Three on-ramps to join

- **Run a Tree** — hold an Orchard Pass → run the oracle → register → store is
  auto-created and `dl_store_id` is bound to the Node (ADR-0002 path (a)).
- **Run a Keeper/Mirror** — `chia data subscribe` + `add_mirror` other stores;
  earn $JUICE for mirror durability (ADR-0002's deferred carrot). Discovery via
  the Orchard Pass NFT collection as the operator directory.
- **Consume** — no permission. Use Atlas, the `orchard-verify` CLI, or a public
  read/export API.

## Consequences

- **Firmware change required** (ed25519 keygen + per-reading signing + pubkey
  export). Trees must be reflashed; `node_id` and key persist across flash.
- **ADR-0001's centralized-trust weakness is reduced**, not just documented:
  device signatures + chain-owned stores move trust to math, and uptime is
  recomputable from public data.
- **Higher write/storage cost** than uptime-only — bounded by batching and the
  configurable cadence (~144 KB/Tree/day of raw readings; trivial at v1 scale,
  measured before scaling — ties to ADR-0002's cost section).
- **Backward compatible:** the `attest:` namespace stays; `node:`, `readings:`,
  and `meta:` namespaces are added. The existing payout `reader.py` keeps
  working against `attest:`.
- **Verification is open:** anyone with a Chia node (or a public RPC) can run
  the full check. We ship `orchard-verify` so *no one has to trust our
  dashboard*.

## Phasing

- **v1 (now):** ed25519 in firmware; writer publishes `meta:`/`node:`/
  `readings:`/`attest:` with real Merkle roots; `orchard-verify` CLI; a local
  Atlas reading the operator's own store.
- **v1.x:** hosted Atlas indexer subscribing across stores; operator directory
  via the Pass NFT; public export API.
- **v2:** Keeper mirror rewards; proof-of-sensing / anti-sybil; environmental-
  oracle consumers (parametric weather/air contracts on Chia); open CHIP-style
  schema; data-DAO governance of reward weights.

## Resolved (frozen 2026-06-09)

- **Season signature:** **ed25519** (symmetry with the device path; publicly
  verifiable against `meta:schema.signer.season_pubkey`). BLS deferred (not
  dropped) for a future on-chain/Chialisp verifier; the oracle signature stays
  so the dashboard has a clean "the oracle claims this summary" statement that
  the public readings then prove or disprove.
- **Reading granularity:** one key per **hour** (`readings:<…>:<HOUR>`), batch
  JSON + app-level Merkle.
- **Beacon source:** oracle **`/beacon`** for v1; direct RPC later.
- **Signed number encoding:** integer fixed-point only; floats rejected in code
  (`schema.reject_floats`).

## Open questions

- **DataLayer write-fee economics at >100 nodes** — needs live measurement.
- **`orchard-verify live`** — exact DataLayer `get_proof` shape + block-anchor
  lookup for on-chain inclusion + anti-backdate (offline Phase 1 already ships).

## See also

- [`docs/datalayer/SPEC.md`](../datalayer/SPEC.md) — concrete key namespace,
  record formats, signing, Merkle construction, frequency, verification, join.
- [ADR-0001](0001-v1-architecture.md) §7 (superseded), [ADR-0002](0002-grove-federation-direction.md).
- Current writer: `orchard_chia/datalayer/` (`attest.py`, `main.py`, `rpc.py`).
