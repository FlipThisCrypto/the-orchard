<!-- SPDX-License-Identifier: Apache-2.0 -->
# T21 — ESP32 on-chain heartbeat client: feasibility spike

> **Status:** feasibility report (HANDOVER T21 explicitly asks for a spike
> *before* implementation). No firmware is written here. The conclusion gates
> whether — and how — to build the on-device spend-bundle path. Hardware
> measurements below are estimates to be confirmed on a WROOM-32 before a
> go decision.

## Question

Can an ESP32 Tree construct, sign, serialize, and broadcast its own
heartbeat-singleton spend (T20 puzzle, ADR-0008 §1) **without an oracle or any
server in the path** — and does it fit on a WROOM-32?

## Headline finding: no on-device BLS needed

The biggest de-risking discovery. A Chia `SpendBundle` carries one aggregated
**BLS** signature covering every `AGG_SIG_*` condition the puzzles emit. The
T20 heartbeat puzzle emits **none** — authorization is the in-puzzle
`secp256r1_verify`, and the other conditions are `ASSERT_SECONDS_RELATIVE` /
`CREATE_COIN` / `CREATE_COIN_ANNOUNCEMENT`. With zero required BLS signatures,
the bundle's `aggregated_signature` is the **aggregate of the empty set = the
BLS G2 identity element**, a fixed 96-byte constant
(`0xc0` followed by 95 zero bytes).

Consequence: the ESP32 needs **no BLS library, no pairing math, no G2 ops** —
historically the scary part of "Chia on a microcontroller." It already has the
only signing primitive required: secp256r1 via mbedTLS (`identity::p256_sign`,
ADR-0007). The device signs the heartbeat digest exactly as it signs readings
today.

This collapses T21 from "port BLS to an MCU" to "serialize CLVM + submit a
transaction" — both tractable.

## What the device must actually do

1. **Know its current coin.** Track the live singleton coin (parent id, puzzle
   hash, amount=1) across heartbeats. Either persist it in NVS after each
   confirmed spend, or re-derive it by querying a node/indexer for the coin at
   its current puzzle hash. (Persisting is simpler; re-deriving is more robust
   to missed confirmations — see open questions.)
2. **Sign the transition.** `digest = sha256(COUNTER, EPOCH, batch, next_ph)`;
   `signature = p256_sign(digest)`. Already have this. The off-device signer in
   `orchard_chia` (`schema.sign_digest`) is the reference; the firmware path
   must produce byte-identical digests (CLVM atom encoding of the integers — see
   `test_heartbeat_puzzle._int_to_atom`).
3. **Compute `next_puzzle_hash`.** Re-curry the heartbeat mod with `COUNTER+1`
   and take its `sha256tree`. This needs a **curry + treehash** routine on
   device (sha256 over the curried structure). ~Tens of sha256 calls over small
   buffers; cheap with mbedTLS SHA-256. This is the main *new* crypto-adjacent
   code, but it's hashing, not signing.
4. **Serialize the CoinSpend** — `(coin, puzzle_reveal, solution)` in CLVM
   serialization, then the `SpendBundle` with the constant empty BLS aggregate.
   CLVM serialization is the classic "atom with length prefix / `0xff` cons /
   `0x80` nil" encoding — see the ~20-line serializers already written twice in
   this repo (`test_clvm_secp.py`, `test_heartbeat_puzzle.py`). Porting that to
   C++ is the bulk of the work but is well-scoped and unit-testable against the
   Python reference vectors.
5. **Submit** the bundle (see transport below) and, on confirmation, advance
   local state to the new coin.

## Memory footprint (WROOM-32, ~320 KB SRAM, ~4 MB flash)

Rough budget — all buffers are small:

| Item | Size |
|---|---|
| Curried heartbeat puzzle reveal | ~0.2–0.5 KB |
| Solution (batch + next_ph + 64-byte sig) | ~0.15 KB |
| Serialized SpendBundle | < 1 KB |
| JSON envelope for `push_tx` | a few KB |
| TLS session (if direct full-node RPC) | **40–50 KB** (the real cost) |

The bundle construction is negligible RAM. The dominant consumer is the **TLS
session**, which the device already pays for posting readings to the oracle
over HTTPS (`oracle.cpp` `WiFiClientSecure`). So the heartbeat path adds little
beyond what 0.4.8 firmware already runs. Verdict: **fits comfortably** on
WROOM-32; the S3 has even more headroom.

## Hardest open problem: the submission transport

Getting the bundle to the mempool is the real design fork, not the crypto.

- **(A) Direct full-node RPC (`push_tx`, :8555).** Authentic, but the Chia RPC
  requires **mutual TLS with the node's self-signed cert**. Pinning a private
  node's cert on every Tree is brittle (cert rotation) and couples each Tree to
  one node. Heavy and fragile.
- **(B) Public submission API.** If a community/maintained endpoint accepts a
  raw `SpendBundle` over plain HTTPS, the device just POSTs JSON — trivial. This
  is the lightest path; depends on such an endpoint existing/being run.
- **(C) Stateless relay.** A tiny public forwarder that accepts a bundle and
  calls `push_tx`. Reintroduces *a* server — but a **stateless, trustless,
  swappable** one (it can't forge or alter a bundle; a bad relay just fails to
  forward, and the device falls back to the next), which is compatible with the
  "no oracle in the reward path" goal. The Tree carries a fallback list and
  rotates on failure.

**Recommendation:** design for a **fallback list of submission endpoints**
(config in NVS) and treat (B)/(C) as the primary path, (A) as an option for
self-hosters. Submission is idempotent (re-pushing the same bundle is safe), so
retry-across-endpoints is simple.

## Timing (uses T6 SNTP, already shipped)

`ASSERT_SECONDS_RELATIVE MIN_INTERVAL` means the spend is only valid once the
parent coin is `MIN_INTERVAL` seconds old. The device uses SNTP wall-clock
(T6) to schedule the attempt just after the window opens, then submits with
retry/fallback. No tight real-time constraint — a late heartbeat just lands
late; it can't be too *early* (consensus rejects it).

## Replay / hardening notes

- The signed digest binds `next_puzzle_hash` + state, but **not the coin id**.
  Cross-coin replay is bounded by the singleton (one live coin per Tree) and by
  the monotonic state, but adding `ASSERT_MY_COIN_ID` (or folding the coin id
  into the signed digest) would make replay structurally impossible. Decide
  when wrapping in `singleton_top_layer` (T20 follow-up).
- Device-key extraction (physical) lets an attacker heartbeat that one Tree —
  see T10 (`docs/security/FLASH_ENCRYPTION.md`). Not a T21 blocker.

## Recommendation

**Feasible — proceed, in this order:**

1. **Desktop signer first** (extend the T11 simulator, `tools/tree_sim`): build
   real heartbeat `SpendBundle`s in Python, submit to **testnet11**, and prove
   a multi-spend lineage end-to-end. This validates serialization, curry/
   treehash, the empty-aggregate bundle, and the transport — *before* any C++.
   It also produces the byte-exact reference vectors the firmware ports against.
2. **Port the serializer + curry/treehash to C++** as a standalone module with
   unit tests against those vectors (no hardware needed for the unit tests).
3. **On-device integration** on a WROOM-32: wire the module to `p256_sign` +
   the SNTP scheduler + the submission fallback list; measure real RAM/timing.
4. **Soak** one real Tree heartbeating its own singleton on testnet11 — the
   project's headline demo (HANDOVER acceptance for the Phase-3 track).

The on-device BLS-free finding makes this a serialization + transport task, not
a cryptography port. Single biggest risk is the **submission transport**, which
is a deployment/ops decision, not a firmware capability gap — settle it during
step 1 on testnet.

## References

- ADR-0008 §1 (heartbeat singleton); `puzzles/src/tree_heartbeat.clsp` (T20).
- `orchard_chia/tests/test_heartbeat_puzzle.py` — the digest/serialization
  reference the firmware must match byte-for-byte.
- `firmware/src/identity.cpp` — `p256_sign` (the only signing primitive needed).
- T6 SNTP (`firmware/src/net/timekeeping.*`) — the timing source.
- `puzzles/SECURITY-NOTES.md` — heartbeat threat model.
