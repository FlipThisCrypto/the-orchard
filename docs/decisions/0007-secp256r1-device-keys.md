# ADR-0007: Device signing curve: secp256r1 (P-256), superseding ed25519

- **Status:** Accepted (2026-06-12) — **time-sensitive: apply BEFORE PR #1 merges**
- **Date:** 2026-06-12
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Supersedes:** the ed25519 device-key choice in ADR-0003 / PR #1
- **Related:** ADR-0008 (serverless target — the reason this matters),
  [`HANDOVER_2026-06-11.md`](../HANDOVER_2026-06-11.md) D2/T18

## Context

PR #1 establishes per-Tree signing keys (ed25519) so readings form a
third-party-verifiable dataset. Research into on-chain verification found a
hard constraint: **CLVM has no ed25519 operator.** Its signature-verification
operators are BLS12-381 (native), plus `secp256k1_verify` and
`secp256r1_verify`, added in CHIP-0011 and available as core CLVM operators
since the CHIP-0012 hard fork. Chia added secp256r1 specifically to support
hardware signers (HSMs, Secure Enclave, secure elements).

ADR-0008 makes on-chain verification of device signatures the project's
target architecture (Tree-as-singleton heartbeats). An ed25519 fleet would be
permanently locked out of that — puzzles could never check a Tree's signature
directly.

## Decision

Tree identity keys are **secp256r1 (NIST P-256), ECDSA over sha256**.

- Firmware: mbedTLS (bundled with ESP-IDF/Arduino core) provides P-256
  keygen, signing, and the TRNG-backed entropy source. No new dependencies.
- Off-chain verification (`orchard-verify`, oracle): standard P-256 ECDSA —
  `cryptography` already in the Python stack supports it.
- Signature encoding: fixed 64-byte r||s (matching `secp256r1_verify`'s
  expected form), pubkey as 33-byte compressed SEC1. Document in the schema.
- Message digest: sha256 of the exact payload bytes (CLVM operator takes a
  digest, not a message — same as our current flow).

## Consequences

**Positive**
- A ChiaLisp/CLVM puzzle can verify a Tree's signature directly
  (`secp256r1_verify pubkey msg_digest signature`) — required for ADR-0008.
- Hardware-security upgrade path: ATECC608-class secure elements do P-256
  natively; a future Tree revision can keep the key in tamper-resistant
  silicon with zero protocol change.
- Same curve class Chia chose for institutional/hardware signing — aligned
  with ecosystem tooling.

**Negative / neutral**
- ECDSA needs a well-formed nonce: use RFC 6979 deterministic ECDSA (mbedTLS
  supports it) to eliminate nonce-reuse risk on-device.
- On-chain verify cost is intentionally higher than BLS — acceptable; a
  heartbeat spend verifies one signature.
- Any test devices already provisioned with ed25519 keys must be
  re-provisioned. Do this before the fleet exists; that's why this ADR is
  time-sensitive.

## Implementation checklist (amend PR #1)

1. Replace ed25519 keygen/sign in firmware identity module with P-256
   (mbedTLS, RFC 6979).
2. Update `orchard-verify` and the dataset schema (curve, encodings above).
3. Update oracle signature check.
4. Add a cross-check test vector: same payload signed on-device and verified
   by (a) Python, (b) a CLVM `secp256r1_verify` call run under the Chia dev
   tools simulator — proving end-to-end on-chain verifiability.
