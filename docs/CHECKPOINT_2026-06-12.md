# CHECKPOINT — 2026-06-12 (follow-up to HANDOVER_2026-06-11)

**Audience:** the Claude session building `FlipThisCrypto/the-orchard`.
**Basis:** an external audit of `main` at commit `bc590b9` ("ci: invoke
pytest as `python -m pytest` for repo-root sys.path").

## Audit result

**Confirmed merged and correct:**
- `.github/workflows/ci.yml` — all four jobs, SPDX headers, pytest
  invocation fixed. T1 ✅
- `.github/workflows/release.yml` — present, untagged so far. T2 (workflow
  half) ✅
- `docs/photos` reduced 9.1MB → 5.8MB. Partial.

**Not found on `main`** (if any of this exists on a branch or open PR, say
so in your status report and reconcile against the items below):
- Replay protection (`seq` / `last_seq` / `require_seq`) — T3
- Curve work of any kind (no secp256r1, no ed25519) — PR #1 appears unmerged
- ADR-0004 / ADR-0007 / ADR-0008 in `docs/decisions/`
- Alembic + WAL (T4), NTP (T6), SECURITY.md + bug template, ruff config,
  README roadmap update, repo description/topics, `puzzles/` toolchain (T19)
- `flasher/*.bin` still committed (expected until first tagged release)

## Directives — execute in this exact order

### 1. Commit the three ADRs (do this first; ~minutes)
Add to `docs/decisions/` with repo SPDX/formatting conventions:
- `0004-central-oracle-v1.md` (per HANDOVER T5 — central oracle as
  TRANSITIONAL bridge, topology, Caddy/tunnel ingress, Season batch flow)
- `0007-secp256r1-device-keys.md` (provided file — curve switch, supersedes
  ed25519; includes implementation checklist)
- `0008-serverless-target-architecture.md` (provided file — North Star:
  Tree singletons, heartbeats, epoch vaults, crank, migration plan)
Until these are committed, the repo you work from does not contain the
project's actual direction. Nothing else proceeds first.

### 2. PR #1: apply ADR-0007, then merge
- Rebase PR #1 onto main (CI now gates it).
- Replace ed25519 with secp256r1 per the ADR-0007 checklist: mbedTLS P-256
  with RFC 6979 deterministic ECDSA in firmware identity; 64-byte r||s
  signatures; 33-byte compressed SEC1 pubkeys; sha256 payload digest;
  `orchard-verify` + schema + oracle check updated.
- Include the cross-check test vector: one device-signed payload verified by
  (a) Python `cryptography`, (b) a CLVM `secp256r1_verify` call under the
  Chia dev-tools simulator. The (b) test is the proof the on-chain door is
  open; do not merge without it.
- Merge only when CI is green.

### 3. Replay protection (T3), gated on step 2
Implement per `replay-protection.md`, adapted to the now-merged secp256r1
payload (the seq counter goes inside the signed body; the scheme is
signature-agnostic):
- Firmware: `next_seq()` NVS reservation-block counter; `payload["seq"]`.
- Oracle: `Node.last_seq`, strictly-increasing check → 409, reset on
  re-registration, behind `require_seq` settings flag (default False),
  mirroring the `require_wallet_session` rollout pattern.
- Tests: replay rejected, out-of-order rejected, re-registration resets.

### 4. Tag v0.4.8 and clean up binaries
- Bump `version.h`, tag `v0.4.8`, push the tag; verify release.yml produces
  the three bins + SHA256SUMS as release assets.
- Then delete `flasher/**/*.bin` from the repo and repoint
  `flasher/manifest.json` at the release-asset URLs.
- If OTA signing (T7/D5) isn't ready, ship this tag unsigned and note in the
  release body that signing lands in the next tag — do not block the bin
  cleanup on it.

### 5. Sweep the small Phase-1 leftovers (one PR is fine)
- README: roadmap checkboxes to actual state (code implements through
  Phase 7) + one paragraph on the new topology citing ADR-0004/0008.
- `SECURITY.md` (disclosure contact) + `.github/ISSUE_TEMPLATE/bug_report.md`.
- `[tool.ruff]` in pyproject (start: `select = ["E9", "F"]`).
- Finish compressing `docs/photos` toward ~2MB if quality allows.
- Remind the owner (cannot be done from the repo): set the GitHub repo
  description and topics (`chia`, `depin`, `esp32`, `iot`,
  `environmental-data`, `blockchain`).

### After these five: resume HANDOVER order
T4 (Alembic/WAL) and T6 (NTP) next, then Phase 2 (T9–T11 tester gate) and
the Phase 3 parallel track (T19 puzzles toolchain onward). The D0 operating
rule from the handover remains in force: minimum oracle investment, maximum
transfer to serverless.

## Status report required
When these five are done, produce `docs/STATUS_2026-06.md` summarizing: what
merged (with PR/commit refs), what's on branches, CI state, and the next
three tasks — so the owner can re-audit without reading diffs.
