# ADR-0005: Remote claim-code provisioning

- **Status:** **Proposed** (2026-06-15) — design for owner review. Defines
  first-boot firmware behavior, which is expensive to change after devices
  ship, so it is captured here BEFORE implementation (HANDOVER T9).
- **Date:** 2026-06-15
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Related:** ADR-0004 (central oracle — why local register no longer
  suffices), ADR-0007 (secp256r1 device key), the Phase 6.6 wallet-session
  auth (`/auth/challenge` + `/auth/verify`), and the Orchard Pass NFT gate.

## Context

With the central oracle (ADR-0004), a remote operator flashes a Tree via the
web flasher and must bind **their** wallet to **their** device on **our**
oracle. The current onboarding doesn't support that:

- It assumes USB-serial + the local dashboard wizard calls `/register` with the
  Tree's `node_id` + signing key. A remote operator has no local oracle.
- `/register` is wallet-session gated (Phase 6.6): the binding wallet is the
  authenticated session's address, not a free-form field. Good — but it means
  the *operator's browser* must do the registration, not the Tree.
- A full ("merged") flash **wipes NVS** (finding 2026-06-15), so every Tree
  reaches the operator unprovisioned: fresh `node_id`, fresh secp256r1 key, no
  WiFi creds, no wallet binding.

So we need a flow where (1) the Tree announces a hard-to-guess identifier the
operator can read, (2) the operator, authenticated with their wallet in a
browser, claims that identifier, and (3) the Tree learns it's been claimed and
starts posting — with no trust placed in the network in between.

## Decision

A **claim-code** flow. Three actors: the Tree, the operator's browser
(wallet-authenticated), and the oracle.

### 1. First boot (Tree, unprovisioned)
- `identity::begin()` already generates `node_id` + secp256r1 key + HMAC secret.
- Derive a short, human-readable **claim code**:
  `base32(sha256(pubkey || boot_nonce))[:8]`, using a no-ambiguous-characters
  alphabet (Crockford-style: no `0/O/1/I/L`). 8 chars ≈ 40 bits.
- Show the claim code **over serial** and on the **WiFi-setup captive-portal
  page** (the operator sees it while entering WiFi creds).
- The `boot_nonce` is persisted so the code is stable across reboots until
  claimed (avoids a moving target), and rotated on successful claim or NVS wipe.

### 2. Claim (operator browser → oracle)
- A hosted claim page (served by the oracle, e.g. `/claim`) where the operator:
  1. authenticates with their wallet (existing `/auth/challenge` + `/auth/verify`
     → session token), then
  2. submits the claim code.
- `POST /provision/claim` (Bearer session) `{ "claim_code": "...." }`:
  - Looks up the unclaimed Tree whose code matches.
  - Runs the **same Pass-NFT verification** `/register` does, binding
    `node_id ↔ session.address` (+ Pass) — reusing that logic, not duplicating it.
  - Marks the claim **consumed** (single-use) and records the binding.

### 3. Activate (Tree → oracle)
- The Tree polls `GET /provision/<claim_code>` on an interval. While unclaimed
  it returns `{"claimed": false}`; once claimed it returns `{"claimed": true,
  "oracle_url": ...}` (and any config the Tree needs), after which the Tree
  stores its binding state in NVS and begins normal signed posting.
- The poll is authenticated by the Tree (HMAC / its node signature) so only the
  genuine device — not a network observer who saw the code — learns the result
  or receives activation config.

### 4. Lifecycle
- Claim codes **expire** (default 24h) and are **single-use**.
- Re-provisioning (NVS wipe / transfer) generates a fresh code and clears any
  prior binding on re-registration (consistent with the `last_seq` reset path).

## Security considerations

- **Guessing:** 8 chars over a ~32-symbol alphabet ≈ 40 bits; combined with
  per-IP rate limiting on `/provision/*` (reuse the existing limiter) and 24h
  expiry, brute force is impractical. Increase length if we want margin.
- **Code interception ≠ takeover:** claiming requires a *wallet session*, and
  activation config is only returned to the *Tree* (authenticated poll). A
  leaked code lets an attacker bind the Tree to *their own* wallet (a griefing
  DoS on that one device), not exfiltrate anything — mitigated by expiry,
  single-use, and the operator visually confirming the code on their device.
- **Race:** two claimants, one code → first consumes it, the rest get
  "already claimed." Single-use is enforced with a guarded update (same pattern
  as the `last_seq` replay check).
- **Transport:** the oracle is TLS-only (Cloudflare tunnel, ADR-0004), so codes
  and sessions aren't on the wire in clear.

## Open questions (for owner sign-off before implementation)

1. **Code length/alphabet** — 8 Crockford chars, or longer? Display grouping
   (e.g. `XXXX-XXXX`)?
2. **Captive portal in v1?** Serial-only first (simplest), portal as a
   fast-follow? Or both from the start?
3. **Pass requirement at claim time** — must the wallet already hold an Orchard
   Pass to claim, or can binding precede Pass mint?
4. **Binding to DID vs wallet address** — T9 says "node_id ↔ DID/Pass";
   confirm whether we bind to a DID or the xch address (current `/register`
   binds the address).
5. **Does activation push the oracle URL / WiFi-independent config**, or is the
   Tree pre-pointed at `oracle.theorchard.network` at flash time?

## Consequences

- Remote operators onboard with **zero local tooling**: flash → read code →
  claim in a browser → Tree self-activates.
- Supersedes local-`/register` as the *operator* path; local register stays for
  dev/bench. First-boot firmware gains a provisioning state machine — the part
  that's expensive to change later, hence this ADR first.
- New oracle endpoints (`/provision/claim`, `/provision/<code>`, claim page) and
  a `claims` table (code, node_id, created_at, expires_at, consumed_at).
