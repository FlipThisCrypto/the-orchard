<!-- SPDX-License-Identifier: Apache-2.0 -->
# Security Policy

The Orchard is a proof-of-concept DePIN with real cryptographic keys, an
internet-facing oracle, and a token with value. We take security seriously and
welcome responsible disclosure.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Email **flipthiscrypto@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce (proof-of-concept welcome).
- The affected component and version/commit (firmware version, oracle commit,
  etc.).

If you'd like to encrypt the report or need an alternate channel, say so in a
first contact email and we'll arrange it.

### What to expect

- Acknowledgement within **5 business days**.
- An initial assessment (severity + whether we can reproduce) within **10
  business days**.
- Credit in the fix's release notes if you'd like it (tell us how to credit
  you, or say you'd prefer to stay anonymous).

This is a small volunteer project in active development — timelines are
best-effort, not contractual.

## Scope

In scope:

- **Firmware** (`firmware/`) — device key handling, signing, OTA, provisioning.
- **Oracle** (`oracle/`) — reading ingestion, wallet-session auth, Pass
  verification, replay protection, rate limiting.
- **Chia integration** (`orchard_chia/`) — DataLayer writer, payout script,
  signature verification, the publish schema.
- **Dashboard** (`dashboard/`) — the local Orchard View UI.

Out of scope (known limitations, documented, not vulnerabilities):

- A Tree that is powered and heartbeating but physically idle still earns
  uptime — addressed by the future Keeper layer, not v1
  ([ADR-0008](docs/decisions/0008-serverless-target-architecture.md)).
- GPS / location spoofing — deferred to the Keeper layer.
- Sensor-data plausibility — v1 rewards are uptime-based by design.

## Known security posture (PoC)

- Released firmware binaries are currently **unsigned**. Verify downloads
  against the `SHA256SUMS.txt` on each
  [release](https://github.com/FlipThisCrypto/the-orchard/releases) and flash
  over USB or trusted OTA only. Signature-verified OTA is in progress (D5/T7);
  the key-handling procedure will be documented in this file when it lands.
- Device keys are secp256r1, generated on-device, private scalar never
  transmitted ([ADR-0007](docs/decisions/0007-secp256r1-device-keys.md)).
  Flash/NVS encryption to protect the key at rest is a tester-readiness item.
- The oracle defaults to loopback binding; production deployment guidance
  (TLS, rate limiting, network isolation) is in the deployment runbook.
- No secrets live in the repo. Wallet config is gitignored; the OTA signing
  key (once it exists) lives only in CI secrets.

## Supported versions

This is pre-1.0 software. Only the latest tagged release receives security
fixes; there are no long-term support branches yet.
