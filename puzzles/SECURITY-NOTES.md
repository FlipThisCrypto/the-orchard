<!-- SPDX-License-Identifier: Apache-2.0 -->
# Orchard on-chain puzzles — security notes

Living security analysis for the value-holding puzzles of the serverless track
(ADR-0008). Scope today: the Tree-singleton **heartbeat** puzzle (T20,
implemented). The **epoch vault** (T22) section ports the prior Merkle-vault
claim-race analysis so it's not re-discovered later; it is design-stage until
that puzzle lands.

> Every value-holding puzzle gets review + a testnet11 soak before mainnet
> (README convention). These notes are the review checklist, not a substitute
> for the soak.

## Heartbeat puzzle (`src/tree_heartbeat.clsp`, T20)

### What the chain enforces
The inner puzzle, wrapped by the standard `singleton_top_layer`, makes each
heartbeat spend:

1. **Tree-authorized.** `secp256r1_verify` (CHIP-0011, opcode `0x1c3a8f00`) of
   the Tree's signature over the transition digest. A spend with no/!valid
   signature raises and is rejected by consensus. The Tree's private key never
   leaves the device (ADR-0007).
2. **Rate-limited.** `ASSERT_SECONDS_RELATIVE MIN_INTERVAL` — consensus refuses
   the spend until `MIN_INTERVAL` seconds have elapsed since the parent coin was
   created. This is the anti-inflation control: the counter cannot be advanced
   faster than wall-clock time, even by the keyholder.
3. **State-bound.** The signed digest is
   `sha256(COUNTER, EPOCH, sensor_batch_hash, next_puzzle_hash)`. Because the
   signature covers all four, a captured signature cannot be replayed into a
   different counter/epoch, re-pointed at a different sensor batch, or
   redirected to a `CREATE_COIN` the Tree did not authorize. (All four
   rejections are covered in `orchard_chia/tests/test_heartbeat_puzzle.py`.)
4. **Observable.** A domain-tagged (`"hb"`) `CREATE_COIN_ANNOUNCEMENT` of
   `(tag, COUNTER+1, EPOCH, batch)` lets the epoch vault count heartbeats by
   asserting the announcement — the tag stops cross-puzzle announcement
   confusion.

### Threat model & residual risks
- **Stolen device key.** An attacker who extracts the key (physical readout —
  see T10 / `docs/security/FLASH_ENCRYPTION.md`) can heartbeat *that one* Tree's
  singleton, but still only once per `MIN_INTERVAL`, and cannot touch any other
  Tree (per-device keys). They cannot mint extra uptime, only continue the
  Tree's existing cadence. Mitigation: flash encryption (T10); the reward
  ceiling per node also bounds the payoff.
- **Counter honesty.** The counter is the Tree's own; the chain enforces *rate*,
  not *truthfulness of the batch contents*. Data-quality grading (ADR-0006) and
  the vault's Pass-NFT gate are the economic backstops, not this puzzle.
- **Off-chain `next_puzzle_hash` computation.** The Tree computes the next coin's
  puzzle hash off-device (re-curry with `COUNTER+1`) and signs it. A bug there
  only strands the Tree's own singleton; it cannot redirect value elsewhere
  (the signature binds the exact next hash). The on-chain curry-and-treehash
  check is deferred — acceptable because the singleton holds only its 1-mojo
  uniqueness amount, not rewards.
- **Singleton uniqueness** is delegated to the audited `singleton_top_layer`
  (odd-amount + lineage proof). The inner puzzle MUST always recreate exactly
  one singleton coin (amount 1); double-CREATE_COIN or wrong amount would be a
  bug to catch in review + the curry/treehash test when wrapping lands.

### Still to do for T20→production
- Curry harness + pinned treehash for a *curried* instance (identity + initial
  state), with a test that the recreation `next_puzzle_hash` equals the
  re-curried hash (closes the "off-chain computation" gap above).
- Wrap in `singleton_top_layer` and exercise a multi-spend lineage on testnet11.
- Desktop signer (extend the T11 simulator) that emits real spend bundles
  (the broadcast path is T21).

## Epoch vault (T22) — ported claim-race analysis (design-stage)

From ADR-0008 §2. Captured here so the known failure modes are designed out
before the vault holds funds; nothing in this section is implemented yet.

- **The race.** A single vault coin paying N claimants invites a race: two
  valid claim spends reference the same vault coin; only one wins per block, the
  losers' bundles are invalidated and must rebuild against the new vault coin,
  repeatedly — griefable, and unfair under congestion.
- **Chosen mitigation: pre-split at epoch close.** One extra spend bundle splits
  the epoch vault into **per-node claim coins** (or per-tier vaults) the instant
  the epoch closes. Each node then claims *its own* coin — no shared coin, no
  race. This is the design ADR-0008 §2 commits to; the Merkle-vault analysis's
  conclusion (isolate claimants onto independent coins) applies directly.
- **Snapshot integrity.** The per-node split must be computed from an
  immutable epoch-close snapshot of heartbeat counts (the announcements above).
  Open questions for T22: where the snapshot commitment lives (a Merkle root in
  the crank spend vs. announced totals), and how a claimant proves membership
  cheaply in CLVM.
- **Timelocked sweep.** Unclaimed per-node coins return to the treasury puzzle
  after the claim window (ADR-0008 §2) — bounds dust and abandoned coins.
- **Pass-NFT gate.** Each claim is gated by ownership of the matching Genesis
  Pass; the ownership proof mechanism (announcement from the Pass coin vs. a
  curried puzzle hash) is a T22 decision.
- **Permissionless crank.** Epoch rollover (ADR-0008 §3) is anyone-can-spend
  with a tip; review must ensure the tip can't be inflated to drain the vault
  and that the crank can't be run early (its own timelock).
