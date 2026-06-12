# ADR-0008: Target architecture: serverless Orchard (Tree singletons, on-chain epochs)

- **Status:** Accepted as North Star (2026-06-12)
- **Date:** 2026-06-12
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Relationship to ADR-0004:** the central oracle is hereby reclassified as a
  **transitional bridge**, not the product architecture. Minimize further
  investment in oracle-only features; maximize work that transfers.
- **Related:** ADR-0007 (secp256r1 — the enabling primitive),
  [`HANDOVER_2026-06-11.md`](../HANDOVER_2026-06-11.md) D0 / Phase 3 (T18–T24)

## Goal

The Orchard should run **without servers and without upkeep**: if the founder
disappears, Trees keep proving uptime, epochs keep rolling, operators keep
claiming rewards, and anyone can audit all of it. Decentralization is the
product.

## Core insight

A blockchain cannot observe sensors or WiFi — but the v1 reward metric is
**uptime**, and uptime can be reframed as something the chain CAN observe:
proof that a device-bound key was alive and acting at enforced intervals.
That reframing removes the oracle from the reward path entirely.

## Architecture

### 1. Tree = singleton coin (heartbeat)
Each Tree owns an on-chain singleton secured by its secp256r1 key (ADR-0007).
The Tree periodically spends the singleton back to itself. The puzzle
enforces:
- `ASSERT_SECONDS_RELATIVE` minimum spacing between heartbeats — elapsed time
  proven by consensus, unfakeable.
- State carried in the singleton: monotonic heartbeat counter (+ epoch id).
  After an epoch, the counter IS the uptime ledger. No server computed it.
- Each heartbeat memo carries sha256 of that interval's sensor batch — an
  immutable, timestamped data commitment.
- Spend authorized via `secp256r1_verify` of the Tree's signature over the
  solution digest.

### 2. Epoch reward vault (on-chain payout)
Per epoch, a vault coin holds that epoch's $JUICE. Claims are pure ChiaLisp:
- A Tree singleton's spend announces (node pubkey, epoch, heartbeat count);
  the vault releases the proportional/tier amount against that announcement,
  gated by ownership of the corresponding Genesis Pass NFT.
- Timelocked sweep: unclaimed funds return to the treasury puzzle after the
  claim window.
- Concurrency: pre-split the vault into per-node claim coins at epoch close
  (one extra spend bundle), or per-tier vaults — eliminates claim races
  (known design concern from the Merkle-vault work; that analysis applies).

### 3. Epoch rollover = crank (anyone-can-spend)
Epoch advancement is a permissionless "crank" puzzle paying a small tip to
whoever turns it after the timelock allows. The system advances whether or
not the founder exists.

### 4. Clients are static
- Status page / network map: static SPA (GitHub Pages) reading chain state
  via public APIs or the user's own node. No backend.
- Claiming: Sage wallet or a static page constructing the spend client-side.
- Registration: replaced by "mint your Tree's singleton" from the operator's
  wallet during claim/provisioning.
- Firmware: already GitHub Pages (flasher) + GitHub Releases (signed bins).

## What honestly remains off-chain (accepted residue)

1. **Broadcast endpoint.** The ESP32 must submit spend bundles to *a* full
   node — any full node: operator-run or public API providers (multiple,
   configurable, with fallback list in firmware). Not a project server.
2. **Raw sensor payload hosting.** On-chain: hashes (free-ish). Payloads:
   DataLayer mirrors (anyone can run) / community pinning / or
   commitment-only for v1. v1 product claim: "verifiable uptime network with
   data commitments."
3. **Physical truth.** A heartbeating Tree in a drawer still earns — same
   weakness as the oracle design, so nothing is lost; Keepers (v2, inherently
   off-chain actors) address it. State this plainly in public docs.
4. **Fees.** Heartbeats are real transactions. Model cost at 100/1,000 Trees
   for candidate cadences (1h/6h/24h). The counter design makes cadence a
   tunable economic parameter, not a correctness one.

## Language decision

Puzzles ship in **ChiaLisp** (battle-tested). Rue (Rigidity's Rust-like
language) is promising but self-described bleeding-edge as of late 2025;
optionally prototype puzzles in Rue for readability/comparison, never as the
deployed artifact for value-holding coins. Revisit when Rue has production
mileage.

## Migration plan (sequenced, not big-bang)

- **Bridge (now):** central oracle (ADR-0004) ships to testers. Everything in
  it that transfers is prioritized: secp256r1 keys, seq counter (becomes the
  heartbeat counter), Merkle vault claims, flash encryption, signed OTA.
- **Parallel track:** singleton heartbeat prototype on testnet — first with a
  desktop signer emulating a Tree, then on real ESP32 hardware.
- **Cutover criteria (oracle decommission):** ≥N epochs of vault claims
  without incident on testnet + mainnet pilot; ≥X% of fleet on
  heartbeat-capable firmware; historical Season ledger published to
  DataLayer as a final archival root. Then the oracle is deleted, not
  maintained.

## Risks

- **Embedded complexity:** building/signing/serializing a Chia spend bundle
  on an ESP32 is novel; memory and BLS-free design must be validated early
  (this is why the curve choice and the testnet prototype come first).
- **Puzzle risk:** value-holding puzzles need review/audit and long testnet
  soak; snapshot/claim-race analyses from prior vault work must be ported.
- **Fee economics:** heartbeat cadence × fleet size × fee market — model
  before fixing cadence in firmware.
- **Public-node dependency:** mitigate with multi-endpoint fallback +
  operator-node support; document how to run your own.
