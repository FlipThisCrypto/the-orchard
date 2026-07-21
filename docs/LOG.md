# The Orchard — Development Log

> Running journal of what worked, what failed, and what we learned. Newest entries on top. Append liberally — failures are as instructive as successes. Format: a date heading, then short bullet points. Keep entries scannable.

---

## 2026-06-12 — Device curve switched ed25519 → secp256r1 before any device shipped (ADR-0007)

- **The catch, in time:** CLVM has no ed25519 operator — its signature ops
  are BLS, secp256k1, secp256r1. The serverless target (ADR-0008: Tree
  singletons verified on-chain) would have been permanently impossible on an
  ed25519 fleet. Caught while PR #1 was still open, so the fix was a rework,
  not a fleet re-key. Lesson: **check the on-chain VM's verb set before
  freezing device crypto.**
- **Determinism preserved across the rewrite:** the golden-vector contract
  ("re-sign ⇒ identical bytes") survives because RFC 6979 makes ECDSA
  deterministic — mbedTLS (`mbedtls_ecdsa_sign_det_ext`) and Python
  (`ecdsa.sign_digest_deterministic`) emit byte-identical signatures for the
  same scalar + digest. Low-S normalization applied on both sides.
- **Firmware got LIGHTER:** the rweather/Crypto dependency is gone — mbedTLS
  ships in ESP-IDF with `MBEDTLS_ECDSA_DETERMINISTIC` already enabled on
  esp32 and esp32s3 targets (checked the bundled sdkconfig.h before writing
  any code — saved a fallback implementation).
- **Bonus:** P-256 is what ATECC608-class secure elements speak natively, so
  the future hardware-key Tree revision needs zero protocol change.

---

## 2026-06-02 (cont.) — "Sensor data not showing" triaged: dashboard is healthy, breakage is device-side; DS18B20 made non-blocking (0.4.7)

Operator report: *"GPS no longer showing in the dashboard with the newest
firmware, neither is the other sensor data — it all worked at one point."*
Resisted touching the dashboard. Measured the data path end-to-end first.

### Evidence (ran the oracle against the live DB, pulled the exact browser JSON)

`GET /api/tree/<id>/latest` for all three nodes:

- **WROOM `5B9BB022` (fw 0.4.3)** — MQ-135 reporting REAL data *right now*;
  GPS `chars_processed: 0` → UART1/GPIO18 receiving zero bytes.
- **S3 `98EA8567` (fw 0.4.0)** — every sensor 0, `ds18b20: read_failed`.
  Flashed before the S3 pin overrides (0.4.3) → wrong pins (ADC on a
  non-existent GPIO 34, etc.).
- **S3 `D8F89B9E` (fw 0.4.5)** — `last_reading_at: null`, never POSTed. The
  0.4.5 power-cycle loop; the 0.4.6 fix was never flashed to it.

**Conclusion: the dashboard, oracle, `/readings`, `/api/tree/latest`, and
app.js render path are ALL correct. 82/82 tests pass. Whatever a Tree
sends, the dashboard renders.** The breakage is 100% on the devices.

### GPS is hardware, not firmware — do NOT rebuild the driver again

- `git diff 50ede87 HEAD -- gps_neo.cpp`: the 0.4.3 driver is byte-identical
  to the working 0.1.0 single-`begin()` path; the only delta is the opt-in
  `#if ORCHARD_GPS_AUTOBAUD` block, which is compiled out by default.
- 0.1.0 built against `espressif32@^6.7.0`; 0.4.x pins exactly `6.7.0`. Same
  floor → not framework drift either.
- A real fix was captured on 0.1.0 (reading id 6119, 2026-05-31) with
  `fix_age_ms` ≈ 33 h — i.e. the module had already gone silent ~05-30.
- `chars_processed: 0` = nothing on the wire. Same symptom as the 2026-05-29
  entry: loose GPS TX→GPIO18 wire / power / antenna. Triage with `GPS_RAW`
  before changing any code.

### DS18B20 non-blocking — the real fix for the S3 power-cycle (0.4.7)

Root cause of the 0.4.5 power-cycle: DS18B20's `requestTemperatures()` blocks
the main task ~750 ms (12-bit conversion). Firing that at boot *during* the
WiFi association handshake (0.4.5 made `wifi_begin()` non-blocking, so the
boot sample fired immediately instead of after connect) browned the chip out
~12 s in. 0.4.6 gated the sample on `wifi_connected()` — a workaround that
left the 750 ms block in place (just moved it past connect).

0.4.7 removes the block at the source:
- `setWaitForConversion(false)` in `DS18B20Sensor::begin()`, kick one
  conversion there; `read()` returns the PREVIOUS conversion's result (always
  done — samples are 60 s apart) and kicks the next. `read()` now returns in
  microseconds. The brownout can't recur even if the WiFi gate were removed.
- Kept the WiFi gate in `main.cpp` as an efficiency guard only (don't sample
  with nowhere to POST); updated its comment so it no longer claims to be
  load-bearing for stability.
- Compiles clean on all three envs (wroom / s3 / s3_uart); `0.4.7` verified
  embedded in both `firmware.bin`s.

### Web flasher was serving 0.3.0 (the broken auto-baud GPS build!)

`flasher/manifest.json` still pointed at `orchard-wroom32u-0.3.0.bin` — the
exact regressed firmware. Bumped to 0.4.7, regenerated the WROOM merged image
(`esptool merge_bin`, `--flash-mode/--freq keep`), added the long-missing
**ESP32-S3** build (`freenove-s3-uart` variant — matches the field boards),
deleted the stale 0.3.0 blob, and updated `index.html` + `README.md`. The S3
web-flash image is new and flagged for a one-time browser smoke test (the CLI
upload path is the verified one).

### Operator actions to close it out (hardware — only the operator can)

1. **WROOM GPS:** monitor + `GPS_RAW`. Silence → re-seat GPS TX→GPIO18,
   check 5V/GND + antenna. Bytes-but-no-fix → antenna/sky view.
2. **Reflash all boards to 0.4.7** (`pio run -e <env> -t upload`); S3 boards
   use `freenove_esp32s3_uart`.
   - `98EA8567` 0.4.0→0.4.7 fixes its pins (MQ135→7, SCL→9, DS18B20→10).
   - `D8F89B9E` 0.4.5→0.4.7 fixes the power-cycle. If it STILL resets ~12 s
     in during connect with no sampling involved, it's a pure power brownout
     → powered USB hub / better cable.
3. Watch `/tree/<id>` — tiles populate within 60 s of the first good POST.

---

## 2026-06-02 — Marathon multi-board bring-up + handover

A long session: Phase 9.0 completion, two S3 boards, two firmware
regressions, a non-blocking WiFi refactor that introduced a power-
cycle loop. Detailed handover doc is at `docs/HANDOVER_2026-06-02.md`.

### What got shipped (in order)

- **Phase 6.6 close** (`201321d`) — public-mode network stats card,
  OPERATOR_QUICKSTART §10 rewrite, 4 unskipped scope tests, cross-tab
  session via localStorage + `storage` event.
- **Phase 9.0** (`18d6bb0`, `6c7ba5c`) — `HW_INFO` serial command on
  the firmware, board + chip + sensor chips on the wizard's Step 1.
  Per-board `ORCHARD_BOARD_HINT` define so each variant is
  self-identifying.
- **GPS thread fix** (`d532bd0` → `432ac62` → `7552004`) — eventually
  reverted the 0.3.0 auto-baud probe back to the 0.1.0 single-begin
  path because the probe's 6-cycle end/begin churn left UART1 in a
  degraded state on the WROOM-32U. Auto-baud is now opt-in via
  `-D ORCHARD_GPS_AUTOBAUD=1` for HiLetgo-style clones.
- **S3 bring-up** (`c3a1b70`, `fd67c73`, `48f6d86`) — added the
  `freenove_esp32s3_uart` env for boards with external CH343 UART
  bridges (vs. native USB CDC), overrode MQ-135 to GPIO 7 (ADC1_CH6
  on S3), and overrode I²C SCL + DS18B20 to S3-valid pins (GPIO 22
  and 25 aren't broken out on the S3 module).
- **Serial console responsiveness** (`8b4e0fe` → `90fb0e5` →
  `5c7ddc3` → `f56b2d9`) — every serial command must ack within
  ~50ms; blocking commands like WIFI_SET were starving the dashboard
  wizard's 3s timeout. Converted to an async state machine. Then
  found that aggressive `WiFi.status()` polling plus the DS18B20
  read at boot was contention-causing a power-cycle loop on the S3.

### Lessons logged (for future me)

1. **The 0.3.0 GPS auto-baud probe was the regression, not the
   framework.** I burned 30 minutes pinning espressif32 to 6.7.0
   before discovering my own probe was the cause. The fix was in
   the 0.3.0 commit message itself — operator caught it: *"Look at
   the repo or the notes or whatever to find the fix."*
2. **Blocking serial command handlers are a footgun.** Anything that
   takes longer than ~50ms in dispatch_() — WiFi connect, DS18B20
   conversion, network POSTs — must ack OK immediately and do the
   slow work via a main-loop state machine. The dashboard's serial
   timeout is 3s by default and operators don't know that.
3. **Non-blocking ≠ free.** Switching from blocking `delay(250)`
   spins to `delay(10)` main-loop ticks means everything runs 25×
   more often. WiFi.status() polled 100×/sec can starve the WiFi
   internal task. Throttling status polls to 250ms (matching the
   old spin cadence) is the fix.
4. **DS18B20 reads block for ~750ms (12-bit conversion).** That
   blocking window overlapping a WiFi connect handshake at boot is
   what triggered the S3 power-cycle loop in 0.4.5. Fixed in 0.4.6
   by gating the sample tick on `wifi_connected()`.

### Two S3 boards, two stories

- **COM11** — flashed 0.4.3 originally, no serial output because
  `ARDUINO_USB_CDC_ON_BOOT=1` was routing Arduino's `Serial` to
  native USB instead of the CH343 bridge on UART0. Fixed the env
  but never reflashed COM11 — moved to COM12. Still on broken fw.
- **COM12** — went through 0.4.3 → 0.4.4 → 0.4.5 → 0.4.6 over
  several reflashes. Provisioned successfully through the wizard
  (the FlipThisOrchard label + node_id D8F89B9E…). After 0.4.5 the
  chip started power-cycling at ~12s after each boot — not enough
  time to connect WiFi. 0.4.6 contains the fix but the upload was
  blocked by an operator's open monitor at session end; **fix is
  unverified on hardware as of handover.**

### Handover state

- Repo: `origin/main` at `f56b2d9`.
- Tests: 82 pass / 0 skipped.
- Live Trees: 1 healthy (WROOM 5B9BB022…, fw 0.4.0 — should be
  reflashed to 0.4.6 for GPS), 1 in limbo (FlipThisOrchard
  D8F89B9E… on 0.4.6, power-cycling pending verification of the
  fix), 1 stale (98EA8567… on 0.4.0).
- See `docs/HANDOVER_2026-06-02.md` for the full snapshot.

---

## 2026-06-01 — GPS regression after 0.4.x rebuild (and lesson)

### What happened

- Tree was running **fw 0.1.0** (the original single-`begin()` hardcoded-9600 path). GPS was producing clean NMEA, fix info reaching the dashboard for hours/days.
- Upgrade to **fw 0.4.0** (Phase 9.0 — new `HW_INFO` serial command, no GPS-code changes per the diff). Auto-baud probe inherited from 0.3.0 ran on boot, logged `passed=0` at every rate, fell back to 9600 with a warning. Dashboard tile started showing `fix: no`, `baud: no lock`, `sentences: 0`, `bad checksum: 19`.
- Three follow-up rebuilds (0.4.1 pinning every PIO dep, 0.4.2 making the probe report failed-checksum too) chased framework version drift theories. **None of those were the cause.**

### Actual root cause

- The 0.3.0 auto-baud probe (commit `08db0ac`) does **six end()/begin()/drain/wait cycles** before settling. On this WROOM-32U, that churn appears to leave UART1 in a degraded state — the **0.1.0 single-`begin()` path** worked cleanly. 0.4.2 probe results made it obvious: framing showed up at 9600 (`failed=5`), 115200 (`failed=3`), AND 4800 (`failed=6`). At a single correct baud you'd see framing at ONE rate. Seeing it at three is statistical noise + half/quarter-rate aliasing — meaning the GPS *is* at 9600 (matches what 0.1.0 expected) but the receiver is too degraded to checksum any of it.
- The probe **locked at 4800** because that rate happened to have the highest framed-but-corrupt count. 4800 is half of 9600. Listening at half-rate on a clean line catches every other bit — perfect recipe for "framing visible, payload garbage."

### Fix (in `432ac62`'s successor)

- Default GPS init back to the 0.1.0 path: single `gps_uart.begin(ORCHARD_GPS_BAUD, ...)` at the hardcoded factory rate. No probe, no end/begin churn.
- Auto-baud is now **opt-in** via `-D ORCHARD_GPS_AUTOBAUD=1` for operators with HiLetgo-style clones at 38400.
- Version bumped to 0.4.3.

### Lesson for future me

- **Read LOG.md and `git log -- firmware/` BEFORE rewriting a probe that already shipped.** I had four separate prior touch-points on this exact subsystem (initial 18/19 pin remap, GPS_RAW diagnostic command, 0.3.0 auto-baud, dashboard tile interpretation). The 0.3.0 commit message literally said the symptom was "Result C from the GPS triage table: $GPRMC headers leaking through pages of garbage" — which is exactly what the operator was seeing again. Half an hour of bad theorizing (framework version drift, library bumps, hardware integrity) could have been one `git log` away.
- The `0.4.1` exact-version PIO pin is still good practice and stays. The `0.4.2` failed/passed diagnostic in the probe is still a useful signal and stays gated behind the opt-in flag.
- **When an operator says "this worked before your change" — believe them and `git diff` the change first, theorize second.**

---

## 2026-05-30 — Genesis collection live on chain

The Orchard — Genesis Passes (10 NFTs) are minted and indexed on MintGarden.

- **Collection (bech32):** `col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29`
- **Collection (CHIP-7 UUID):** `96ae1978-1a69-4f1c-ad24-f5ac66d02811`
- **Browse:** https://mintgarden.io/collections/col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29
- **Creator DID:** `did:chia:10g777py7u3yj2uytdd7a0537ajkkdap9yk9jau5g7n27vvf3s7jqrfamq3`
- **Minter address:** `xch1yq9grysxg3tjx5drgjg2f9rps9njd34gl3c0g5x3gyhyq9lmyhlqhh8997`
- **Royalty:** 10% (1000 BP).
- **Per-Pass `data_hash` and `license_hash`:** match the values in our `nft/mint_plan.yaml` byte-for-byte — same compressed videos, same License PDF as we prepared locally.

### What went wrong with our own tooling

We made two mint attempts via `orchard_chia.nft mint`:

1. **First attempt** — failed all 10 with `Failed to convert b'' from type bytes to bytes32`. Root cause: Chia's wallet RPC parses `license_hash` as bytes32 and rejects empty strings. Fixed in `wallet/rpc.py` by only including `license_uris` / `license_hash` in the request body when non-empty.

2. **Second attempt** — Pass 1 minted successfully (the on-chain Pass that became one of the two we later burned), but Passes 2-10 failed with `DID is not currently spendable`. DID-bound NFT wallets re-spend the DID coin on every mint and the replacement coin doesn't become spendable until the previous mint confirms. Fix attempt: refactored `mint_batch` to call `nft_mint_bulk` (single tx → DID spent once).

3. **Third attempt (nft_mint_bulk)** — the bulk endpoint produced one duplicate of Pass 1 instead of the intended editions 2-10. Inferred cause: interaction between the per-item `edition_number` in `metadata_list` and the top-level `mint_number_start`. We could not get consistent behavior out of `nft_mint_bulk` on the DID-bound wallet within a reasonable iteration window.

After attempt 3, we had 2 broken on-chain NFTs (both Pass #0001, old metadata that included `series_number` / `series_total` / `sensitive_content` — fields MintGarden's parser appears to choke on, causing video playback to fall back to image-only).

### What worked

Richard burned the 2 broken NFTs and minted all 10 via **mintgarden-studio** (the web UI). It handles bulk minting through its own backend without exposing the DID-spendability foot-gun.

### Code we kept anyway

- **`build_pass_metadata()`** generator — still useful for future collections (Beta, Season 2 prestige) where we don't want a hosted-studio dependency.
- **`mint_batch()`** with individual `nft_mint_nft` + 90 s delay — proven path for single mints; useful for Pass-style emergencies and for the Phase 7 $JUICE CAT spends.
- **`nft_transfer_nft` wallet RPC** — needed for any future operator-to-operator NFT delivery.
- **`mint_plan.yaml`** — the operator's record of what each Pass was supposed to be. data_hash/license_hash values matching the on-chain ones is itself a useful provenance artifact.

### Code that we updated to reflect the on-chain reality

- `ORCHARD_GENESIS_COLLECTION_ID` → `"96ae1978-1a69-4f1c-ad24-f5ac66d02811"` (was a placeholder UUID we generated locally).
- Added `ORCHARD_GENESIS_COLLECTION_BECH32_ID` constant.
- Added `ORCHARD_GENESIS_CREATOR_DID` constant.
- `ORCHARD_GENESIS_COLLECTION_NAME` switched from em-dash to ASCII hyphen ("The Orchard - Genesis Passes") to match mintgarden-studio's encoding.
- `ORCHARD_GENESIS_WEBSITE` switched from the GitHub repo to `https://fiendstudios.com/` (matches the on-chain `website` attribute).
- `verify.py` now accepts either the UUID or the bech32 id when matching ownership — different wallet RPCs and indexers surface different forms of the same collection.

### Open items

- The minting wallet (`xch1yq9grys...`) is mintgarden-studio's; the 10 Passes need to be transferred to the operator's main wallet (`xch1m3rvtj86...`) before they can be used as Tree credentials. Use the mintgarden-studio UI or `nft_transfer_nft`.
- 10% royalty is a mintgarden-studio default — confirm whether that's what we want for the genesis batch (DePIN founder-credential NFTs are often 0% royalty; 10% would mean the project takes 10% of any secondary sale).
- The two burned NFTs are on chain forever, but no longer in any wallet.

---

## 2026-05-29 — Orchard Pass collection identity (banner + icon)

Richard produced and uploaded a banner image and a collection icon to his Filebase IPFS bucket:

- Banner: `https://defiant-black-skink.myfilebase.com/ipfs/QmNvG6xqzPGbH31ZS6wNomAJTSqEFsp43t7CHXaZtKxHmb`
- Icon:   `https://defiant-black-skink.myfilebase.com/ipfs/QmUWhqeByfKrVAa5Ev3MRymFmhMSoTMnzDwE3Gjd4Cvray`

Added as constants in `orchard_chia/nft/generate.py`. New helper `_collection_attributes()` returns the standard CHIP-7 collection attribute list, used by both `build_collection_metadata()` (for the standalone `nft/collection.json`) and `build_pass_metadata()`'s `collection.attributes` field (so every per-Pass JSON also carries the banner/icon).

Per-Pass inclusion is the maximum-compatibility move — marketplaces like MintGarden and Spacescan that read the per-NFT metadata's collection block will pick up the visuals automatically without a separate "register your collection" step.

Including the banner/icon in each per-Pass JSON changes the bytes → changes the SHA-256 → invalidates the previous `meta_hash` values in `nft/mint_plan.yaml`. Regenerated all 10 metadata files via `python -m orchard_chia.nft generate` and recomputed all 10 hashes; mint_plan updated.

Caught at the right time — we hadn't actually minted yet, so the genesis batch will go on-chain with banner/icon baked in from day one rather than needing a v2 collection later. If we'd already minted, the on-chain NFTs would reference the old hashes (pre-banner/icon) and we'd need to mint a v2 collection.

Operator's next action: upload the regenerated metadata JSONs at `nft/metadata/0001..0010.json` to Filebase, paste the 10 CIDs back, validate, mint.

## 2026-05-29 — Phase 7: Season harvest ($JUICE payout)

The v1 economic loop is now structurally complete. Phase 7 reads every signed attestation Phase 5 publishes to DataLayer, verifies each one with the oracle's signing key, computes per-Tree rewards, aggregates per recipient wallet, and (in live mode with explicit confirm) sends $JUICE via the Chia reference wallet's `cat_spend` RPC.

### Shipped

- **Reader** (`orchard_chia/payout/reader.py`) — discovers every attestation key in the configured DataLayer store via `get_keys`, filters to the `attest:<NODE>:<SEASON:08d>` shape, fetches and parses each value. Hex-decoded ASCII keys make on-chain inspection trivial.
- **Calculator** (`orchard_chia/payout/calculator.py`) — pure functions. `juice_mojos_for_attestation(attest, daily_rate)` returns CAT mojos. `aggregate_by_wallet(rows)` sums per recipient. v1 math is exactly the spec from ADR-0001: `mojos = round((hours/24) * daily_rate * 1000)`. Future multipliers (Pass tier, sensor diversity, geographic scarcity, reputation) slot in here.
- **Watermark** (`orchard_chia/payout/watermark.py`) — local SQLite at `orchard_chia/data/payout_watermark.db` (gitignored). Records every `(node_id, season) -> (paid_mojos, paid_at, tx_id)`. `INSERT OR IGNORE` makes double-record a no-op; existing rows always win. Lose the file → worst case is a duplicate payment; recommendation in the README is to back it up.
- **CAT spender** (extensions in `orchard_chia/wallet/rpc.py`) — `get_wallets(type=6)`, `cat_get_asset_id`, `find_cat_wallet_id_by_asset`, `cat_spend`. Finds $JUICE by asset_id rather than hard-coded wallet_id so the script works regardless of the operator's wallet ordering.
- **DataLayer `get_keys`** added to `orchard_chia/datalayer/rpc.py` so the reader can enumerate without prior knowledge.
- **Orchestrator** (`orchard_chia/payout/main.py`) — reads attestations, builds a plan with one row per (node, season) and a `status` per row (`ready`, `skipped:bad_sig`, `skipped:already_paid`, `skipped:no_wallet`, `skipped:zero`), renders a human-readable table, aggregates per wallet, and either prints the dry-run summary or interactively confirms before calling `cat_spend` per recipient.
- **CLI flags:** `--confirm` (interactive PAY prompt), `--yes` (skip prompt for cron), `--fee MOJOS` (XCH network fee), `--memo TEXT` (attached to each spend), `--plan-out PATH` (dump plan JSON), `--watermark PATH` (override SQLite location).
- **18 tests** in `orchard_chia/tests/test_payout.py`: calculator at boundaries (0h, 1h, 12h, 24h, scaled rate, negative reject, out-of-range reject), per-wallet aggregation, watermark insert/read/idempotency/persistence/totals, reader key-decode round-trip + rejection cases. 57/57 across all components.

### Decisions

- **Dry-run is the default.** Running `python -m orchard_chia.payout` with no flags reports what *would* be paid and exits with the watermark untouched. Real spends require `--confirm` (interactive PAY prompt) or `--yes` (no prompt — meant for cron).
- **One `cat_spend` per recipient**, not a single batched multi-output spend. Easier to read, easier to debug, easier to retry one failure without retrying the whole batch. Can move to `send_transaction_multi` later if fee minimization matters.
- **Trees without `wallet_address` set are silently skipped** (`status: skipped:no_wallet`). Common when an operator registered before binding a wallet; payable in a later run once they fill it in. No error, no double-spend risk.
- **Signature verification is mandatory** — any attestation that fails `verify_signature` with the oracle's current key is dropped (`status: skipped:bad_sig`). Tampered or key-rotated entries never reach the spend stage.

### What's deferred to v1.1+

- **Tier multipliers** for Orchard Passes (Bronze/Silver/Gold) — calculator interface already accepts the attestation dict, so it's a one-spot change.
- **Cross-machine NFT verification** at the oracle's `/register` endpoint (Phase 6.5) — local-wallet check works; production needs Spacescan/Mintgarden or a signed challenge flow.
- **Batched multi-output `cat_spend`** for fee efficiency at scale.
- **Cron / Task Scheduler example** for `--yes` runs.

### Running it (when DataLayer has attestations)

```powershell
# Dry-run — shows the plan, no chain action
python -m orchard_chia.payout

# Interactive confirm — prompts for PAY before sending
python -m orchard_chia.payout --confirm

# Headless — no prompt, sends immediately. Use in cron once you trust it.
python -m orchard_chia.payout --yes --fee 0
```

---

## 2026-05-29 — Phase 6: Orchard Pass NFTs

Richard's direction: **mint 10 video NFTs as the first 10 Season Passes** — credentials with real artistic identity, not just functional metadata. Each Pass is a short video; holding a Pass is the on-chain claim that lets a wallet register a Tree and harvest $JUICE.

### Shipped

- **Wallet RPC client** at `orchard_chia/wallet/rpc.py` — TLS-wrapped HTTPS to the reference wallet on port 9256, mutual cert auth. Surfaces just what we need: `get_wallets`, `get_next_address`, `nft_mint_nft`, `nft_get_nfts`, `nft_get_info`. Reusable by Phase 7 payout.
- **CHIP-7 metadata generator** at `orchard_chia/nft/generate.py`. Pure functions: `build_collection_metadata`, `build_pass_metadata`, `canonical_json`, `sha256_hex`, `sha256_of_file`, `write_genesis_batch`. Genesis collection id locked to `f9a0c0a0-0001-4000-8000-000000000001` so every Pass and the on-chain ownership check reference the same value. Genesis attributes per Pass: `Pass Number`, `Generation=Genesis`, `Tier=Founder`, `Reward Token=$JUICE`, `Node Type=ESP32-class Tree`, `Network=Chia Mainnet`.
- **Mint pipeline** at `orchard_chia/nft/mint.py`. Reads a YAML mint plan (per-Pass URIs + hashes), validates it (address shape, hex-length, missing URIs, duplicate edition numbers, missing metadata files), calls `nft_mint_nft` for each entry, writes per-mint result to `nft/mint_results.json`.
- **Mint plan template** at `nft/mint_plan.example.yaml` with all 10 passes prefilled with placeholders. Operator copies to `mint_plan.yaml`, fills URIs + hashes after uploading to IPFS / nft.storage / Pinata.
- **Verify helper** at `orchard_chia/nft/verify.py` — pages every NFT in a wallet, returns Passes by matching the collection id. v1 limitation: works only when operator's wallet and oracle's wallet daemon are on the same machine; v1.1 would use Spacescan / Mintgarden for cross-machine.
- **CLI entry point** at `orchard_chia/nft/__main__.py` — subcommands `generate`, `validate`, `mint`, `verify`. Documented in `nft/README.md`.
- **Generated content**: `nft/collection.json` + `nft/metadata/0001.json..0010.json` written by `python -m orchard_chia.nft generate`. Committed so anyone forking the repo can see exactly what the Genesis batch describes.
- **13 hermetic tests** in `orchard_chia/tests/test_nft.py`. All 39 tests pass across all components (oracle 6, dashboard 11, datalayer 9, nft 13).

### Decisions

- **Mint all 10 to issuer wallet first**, then distribute via standard NFT transfers as operators register. Cleaner than collecting 10 recipient addresses upfront, and matches typical Chia genesis batch patterns.
- **Royalty 0%** because credentials shouldn't be priced as collectibles, but every parameter is per-plan-overridable.
- **Soft separation between content (`nft/`) and behavior (`orchard_chia/nft/`).** The JSON files in `nft/metadata/` are committed artifacts so anyone can verify their content matches what gets minted on-chain. The Python is what regenerates and ships them.
- **Oracle `/register` gate deferred to Phase 6.5** — the verify helper is ready but the oracle integration adds a chunk of wiring and depends on a cross-machine ownership story we don't have yet. Keeps Phase 6 tight.

### Awaiting operator action

- 10 short videos.
- Upload to IPFS (recommended: nft.storage).
- Compute SHA-256 of each video and each metadata JSON.
- Fill `nft/mint_plan.yaml` with URIs + hashes.
- `python -m orchard_chia.nft mint --plan nft/mint_plan.yaml`.

Once those 10 mints land, The Orchard has its founding credentials on-chain.

---

## 2026-05-29 — 🌳 First on-chain attestation landed on Chia DataLayer

```
[orchard.attest] DataLayer batch_update accepted.
tx_id = 0x0b94a6951c777453936044188b34cfc904a30d909bfdfa7a281badebd1fea171
```

**Phase 5 went live end-to-end on the real Chia mainnet.** The Orchard published its first signed Season uptime attestation to DataLayer store `d0bb705ed0f9e32fcdae20467e3d64e6aedd9d957b494ae4377ab9c381fd2e37`.

### What landed

- Tree `5B9BB022649FA93D4091DA4BA40714B9` — Season 2, **4 hours of verified uptime**
- Signed with the oracle's HMAC-SHA256 key
- Recorded against chia mainnet block height 8,794,728
- Key in DataLayer: `attest:5B9BB022649FA93D4091DA4BA40714B9:00000002`

### Two problems discovered during the live run, both fixed

1. **Folder-name collision: our `chia/` shadowed (or was shadowed by) the installed `chia-blockchain` package.** Richard's machine has chia-blockchain installed (it has to be — it provides the full-node + DataLayer service). Python found the installed `chia` package at `C:\Python314\Lib\site-packages\chia\__init__.py` first, our local `chia/` had no `__init__.py`, namespace-package vs regular-package rules made the installed one win, and `python -m chia.datalayer` failed with `No module named chia.datalayer`. **Fix:** renamed our folder `chia/` → `orchard_chia/`. All internal imports use relative form so they kept working; the test file (`from chia.datalayer import attest`) was updated to `from orchard_chia.datalayer import attest`. pyproject.toml testpaths updated; .gitignore updated; main README + JUICE.md + module README cross-references updated. Added an `orchard_chia/__init__.py` for good measure so future imports never get tangled with chia-blockchain namespace package detection.

2. **Folder rename was blocked by a file handle.** Initial `git mv chia orchard_chia` failed with `Permission denied`. Diagnosis: Notepad++ had `chia/config.yaml` open AND something else was holding the chia/ directory itself (probably an unrelated explorer window or a stale cwd). Workaround: moved contents file-by-file with `Move-Item`, which the OS permitted; the now-empty folder was then deletable.

### Decision

**Lock in `orchard_chia/` as the permanent name** for our Chia integration package. Anyone replicating this build will have chia-blockchain installed and would hit the same shadowing problem. Documented the rationale in `orchard_chia/README.md`.

---

## 2026-05-29 — BME280 + GPS investigation deferred until new sensors arrive

### Where we left it

- **MQ-135** wired and producing real ADC values continuously. Live view shows real numbers, oracle is storing them, dashboard rendering them every poll.
- **BME280** wired but `active=no` in the firmware boot log — driver's `begin()` probed both `0x76` and `0x77` and got no ACK. Means the chip isn't electrically on the I2C bus despite the wires being on the right pins (GPIO 21/22). Cause is somewhere on the BME280-side wiring (power voltage wrong, SDA/SCL crossed at the sensor, or sensor unit is bad).
- **GPS** wired but `satellites=0`. `GPS_RAW` console command showed 3 seconds of complete silence on the UART — chip is receiving zero bytes from the GPS module's TX pin. Cause is GPS-module-side (antenna unplugged, no power, dead module, or TX/RX swapped at the sensor).
- **Both pre-existing sensors are on the way out**; new BME280 + GPS modules are on order. Hardware investigation parked until they arrive.

### Diagnostic infrastructure landed this session

- `I2C_SCAN` console command — probes every I2C address 1..126, prints which ones ACK. Used to confirm the BME280 isn't on the bus regardless of where the wires are pointing.
- `GPS_RAW` console command — drains the GPS UART, then streams 3 seconds of raw bytes straight to the host. Used to distinguish "GPS wires wrong" (silence) from "GPS antenna missing" (sentences but no fix) from "wrong baud rate" (garbled bytes).
- `pio device monitor --filter send_on_enter` is the right tool for ad-hoc testing — typing commands interactively works, where one-shot Python scripts time out before slower commands finish.

Both new commands ship as part of the firmware and will be useful when the new sensors arrive — no re-flash required to debug the next round.

### Decision

- **Pivot to Phase 5 (Season attestation writer).** The attestation writer reads from the oracle's uptime buckets, which are already accumulating — it does not need GPS or BME280 data. We can build it in parallel with the sensor delivery and have everything in place when the hardware arrives.

---

## 2026-05-28 — 🌱 End-to-end loop closed on real hardware

**Tree node_id: `5B9BB022649FA93D4091DA4BA40714B9`** — running fw 0.1.0 (new firmware with BME280 driver + GPS on GPIO 18/19), POSTing signed readings every 60 seconds, oracle storing them in SQLite, Orchard View polling and rendering them in the browser. **The full v1 proof of concept works.**

### First nine real readings (UTC, 60s cadence)

```
2026-05-28T20:43:27.781196
2026-05-28T20:44:27.683225
2026-05-28T20:45:27.666578
2026-05-28T20:46:27.580423
2026-05-28T20:47:27.594094
2026-05-28T20:48:27.763037
2026-05-28T20:49:27.667321
2026-05-28T20:50:27.637140
2026-05-28T20:51:27.614515
```

Cadence is rock-solid — the firmware's `ORCHARD_SAMPLE_INTERVAL_MS = 60000` loop is honored to the second.

### What's working end-to-end

- **Tree firmware on real ESP32-WROOM-32U** — boots cleanly, identity persists in NVS, all three sensor drivers self-register, samples on schedule, signs each payload with HMAC, POSTs over WiFi.
- **WiFi connection** — Tree's `wifi_mgr` connects to the operator's home SSID with rssi in the -40 to -50 dBm range.
- **Oracle FastAPI** — listening on 0.0.0.0:8000, accepts signed POSTs (HTTP 202), stores in SQLite at `oracle/data/orchard.db`.
- **Per-Tree HMAC verification** — every POST signature verifies against the secret captured at registration.
- **Orchard View dashboard** — home page shows Tree in the registered-nodes list, live view polls every 5 seconds and renders MQ-135, BME280, and GPS cards plus a recent-readings table.
- **Per-hour uptime tracking** — first Season Hour bucket populated; uptime shows "1 / 24 hours" within the first hour.

### Sensors connected vs. reported

- **MQ-135** — not wired this session; firmware reports floating-pin reads (mostly 0.0 with occasional spikes). Expected. Active=yes because the analog driver has no presence probe.
- **BME280** — not wired this session; driver's `begin()` probed both 0x76 and 0x77, got no ack, returned false → registry marks inactive → no card data. **Correct graceful absent-sensor behavior.**
- **GPS NEO-6M with corded antenna** — wired (VCC=5V via breakout, GND via breakout, TX→chip GPIO 18, RX→chip GPIO 19). Active=yes from the registry but `satellites: 0` and `age_ms: 0` — meaning the chip's UART RX is receiving zero NMEA. Separate investigation (probably antenna unplugged, TX/RX swapped, or the GPS's onboard 3V3 regulator not seeing 5V cleanly). Doesn't block the loop.

### Failures / issues encountered and resolved on this session

- **Two `tests/test_basic.py`** modules colliding in pytest's importer — fixed with `pyproject.toml` `--import-mode=importlib` + rename to `test_oracle.py` / `test_dashboard.py`.
- **`std::make_unique` requires C++14** but Arduino-ESP32 v2.x defaults to gnu++11 — fixed with `-std=gnu++17` + `-build_unflags=-std=gnu++11`.
- **Wrong board target** — the prototype is ESP32-WROOM-32U (classic), not S3. esptool's chip-ID guard caught the mismatch before any bytes were written.
- **Dual-target build env added** — `freenove_esp32_wroom` (default) + `freenove_esp32s3`. Status LED differs (GPIO 2 vs 48); GPS pins differ via build flags (18/19 vs 4/5).
- **Auto-reset wasn't triggering download mode** on the WROOM board — documented BOOT-button-hold procedure as the standard.
- **DTR/RTS pulse on dashboard's serial-port open was rebooting the chip** — fixed with `dtr=False, rts=False` set before opening the port in `tree_serial.py`.
- **`flash read err, 1000` bootloop** on the breakout — initially diagnosed as GPIO 12 strapping pin. eFuse summary later showed `XPD_SDIO_FORCE=True` already set — GPIO 12 was a red herring. With all sensors disconnected the chip boots cleanly on the breakout; the actual bootloop trigger was one of the sensor wires being on a misread S3-column pin label.
- **Dual-purpose breakout silkscreen** — Freenove "Breakout Board for ESP32/ESP32-S3 v1.1" has TWO labels per hole (S3 column + ESP32 column). Operator was reading the S3 column but had a classic ESP32 installed. The GPS wires were going to NC holes on the ESP32 side. Re-routed: GPS through chip pins 18/19 directly, power via the breakout's 5V/GND.
- **Windows Firewall blocking inbound 8000** — added `New-NetFirewallRule ... -LocalPort 8000 -Profile Private`, later added a Public-profile rule too in case the WiFi adapter ever gets reclassified.
- **Tree was POSTing to the `.env.example` placeholder IP** — pydantic-settings reads `dashboard/.env`, not `dashboard/.env.example`. Operator edited the wrong file → dashboard used hardcoded default → wrong URL pushed to Tree at provisioning. Fixed by sending `ORACLE_SET http://<oracle-host-on-LAN>:8000/readings` over serial. Operator separately copied `.env.example` → `.env` so future provisionings push the right URL.

### Decisions
- **Phase 9 (breakout integration task) closes with the loop closed.** GPS data path is still pending but is a sensor-side investigation, not blocking.
- **`.env.example` is the template; `.env` is what gets read.** This needs to be more prominent in the dashboard README quickstart.
- **The boot-mode auto-reset issue and the dual-label breakout are both worth documenting** in `docs/wiring/` as gotchas for the next operator.

### Carry-over (next session)
- *GPS troubleshooting*: confirm antenna connection, check GPS LED state, try swapping TX/RX wires (module-perspective vs chip-perspective).
- *MQ-135 / BME280 reconnect*: rewire after GPS works to validate I2C + analog paths.
- *`received_at` UTC serialization*: timestamps come back without an explicit `Z` / `+00:00` suffix. JS `Date.parse()` then treats them as local time, which is why the "Alive" indicator can disagree with "Last reading: just now". Small fix in the oracle response model.
- *Dashboard quickstart docs* — make it explicit that you must `cp .env.example .env`.

---

## 2026-05-28 — Bring-up against the Freenove dual-purpose breakout

### What happened (in order)

- **Closed the loop in software but not in hardware.** Provisioned the Tree end-to-end via Orchard View: register → push WiFi → push oracle URL → SAMPLE_NOW. Three checkmarks, then the fourth came back with the Tree saying `[oracle] POST error: connection refused`. That message was the giveaway that the firmware was actually doing its job; the failure was host-side.
- **Windows Firewall was blocking inbound 8000.** Tree on WiFi could reach the host IP but TCP got refused. Added a firewall rule: `New-NetFirewallRule -DisplayName "Orchard Oracle 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private` (admin PowerShell). Loop's host side then complete.
- **But no POST ever landed.** Captured 12s of the Tree's serial → dead silence. Capture without DTR/RTS reset confirmed the chip wasn't booting at all — `rst:0x10 ... boot:0x13 ... flash read err, 1000 ... ets_main.c 371` repeating every ~370ms — classic ESP32 bootloop where the ROM bootloader can't read the second-stage bootloader from flash.
- **Tracked it to a strapping pin.** "flash read err 1000" with `boot:0x13` is the textbook signature of **GPIO 12 (MTDI) pulled HIGH at boot** — the chip then mis-configures itself for 1.8V flash voltage when our 3.3V flash chip can't be read at that voltage.
- **Re-flashed with explicit `dio` + 40MHz** as a flash-mode workaround. The PlatformIO `esp32dev` defaults of `qio` + 80MHz are marginal on some Freenove WROOM boards. No effect on the bootloop — confirming the issue is strap, not mode.
- **Richard's debugging insight cracked it open:** "When I unplugged it from the breakout, it was able to read." So the chip itself is fine; the breakout PCB has something tied to GPIO 12 that pulls it HIGH at boot.
- **And:** "I gave you the ESP32-S3 labels, my bad." The breakout is a **Freenove Breakout Board for ESP32/ESP32-S3 v1.1** — every header hole has TWO silkscreened labels, one for ESP32-S3 and one for classic ESP32. The user's wiring was done against the S3 column but the chip installed is a WROOM-32 (classic).

### Decoded the pin map from the photos

ESP32-side GPIOs actually exposed on this breakout:

- **Left header**: VP(36), VN(39), 34, 35, 32, 33, 25, 26, 27, 14, 12, 13
- **Right header**: TX(1), RX(3), 23, 22, 21, 19, 18, 5, 4, 0, 2, 15
- **NOT exposed**: 16, 17 (the textbook UART2 defaults! Forced to remap.) Also 6-11 (internal SPI flash, can't use anyway).
- Rows labeled `*` on the ESP32 column are NC on this side — those positions only connect when an S3 is socketed.

The current user wiring:

| Wire | S3-column label (what they read) | ESP32-column label | Actually connected to on this WROOM? |
|------|---------------------------------|---------------------|--------------------------------------|
| GPS Tx → Tree RX  | `36` | `*` | **Nothing** (NC on ESP32 side) |
| GPS Rx ← Tree TX  | `35` | `*` | **Nothing** (NC on ESP32 side) |
| MQ-135 analog     | `6`  | `34` | **GPIO 34** — correct by coincidence ✓ |
| BME280 SDA (yellow)| `21` | `21` | **GPIO 21** ✓ |
| BME280 SCL (green) | `22` | `22` | **GPIO 22** ✓ |

So GPS hasn't actually been wired into the chip *at all* — anything we thought we saw earlier was either an S3 chip in this position, or floating-pin noise. MQ-135 + BME280 are correctly wired on the ESP32 column.

### Fixes shipped in this commit

- **`firmware/platformio.ini`** — under `[env:freenove_esp32_wroom]`, build-flag overrides: `-D ORCHARD_PIN_GPS_RX=18 -D ORCHARD_PIN_GPS_TX=19`. GPS UART moves off the inaccessible S3-style holes onto two real output-capable pins in the ESP32 column right header. S3 env is untouched (still 4/5).
- **`firmware/src/sensors/bme280.{h,cpp}`** — new driver, self-registering via `AutoRegister<>`. Tries I2C address `0x76`, falls back to `0x77`. Reports `temperature_c`, `humidity_pct`, `pressure_hpa`, `i2c_addr`. Returns false from `begin()` if neither address responds, so it auto-skips when not present.
- **`Adafruit BME280 Library@^2.2.4`** added to `[env].lib_deps` (pulls in Adafruit Unified Sensor + BusIO as transitive deps).
- **`dashboard/.../tree.html`** — added a third sensor card for BME280.
- **`dashboard/.../app.js`** — render `temperature_c / humidity_pct / pressure_hpa / i2c_addr` into the BME280 card.

### Still required from the operator (Richard)

1. **Physically move the GPS wires** from the S3-column `36`/`35` holes to the ESP32-column **`18`** (Tree RX from GPS TX) and **`19`** (Tree TX to GPS RX) holes — both in the right header.
2. **Burn eFuse** to permanently lock flash voltage to 3.3V on this specific chip, so GPIO 12 stops being a strap and the breakout's indicator LED on that row stops triggering bootloops:
   ```
   python -m esptool --port COM4 --before no-reset set_flash_voltage 3.3V
   ```
   (Chip must be in download mode: BOOT held, RESET tapped, BOOT still held.)
3. **Re-flash** the WROOM env (same BOOT-button procedure) — picks up the new GPS pins + the new BME280 driver.
4. **Watch the live view** at `/tree/<node_id>` — MQ-135 + BME280 + GPS should all populate within 60 seconds.

### Lessons / observations worth pinning

- Dual-purpose breakouts with "[S3 label] / [ESP32 label]" silkscreens are a sharp edge. Future docs should specify *which column* a pin number refers to whenever giving wiring instructions.
- `flash read err, 1000` + `boot:0x13` repeating ≈ GPIO 12 high at boot. Reach for the eFuse `set_flash_voltage 3.3V` fix when this happens on a board you can't easily depopulate.
- Self-registering sensors continue to pay off — adding BME280 was just dropping in two files. No central registration, no main.cpp change.

---

## 2026-05-27 — Phase 4: Orchard View dashboard

### Successes

- **Orchard View MVP shipped.** Flask 3.1 + vanilla JS + a hand-rolled dark CSS theme (no framework deps). Three pages — home (oracle status + Tree list), Plant a Tree (provisioning wizard), live Tree view (5-second polling).
- **End-to-end provisioning wizard works.** Single-page flow: pick a COM port → identify Tree (PING/NODE_ID/KEY/STATUS over serial) → fill in label/wallet/SSID/password/oracle URL → click Provision → dashboard sequences (register with oracle, WIFI_SET, ORACLE_SET, SAMPLE_NOW), shows status per step, redirects to the live view. No page reloads, no juggling of multiple windows.
- **Live view shows the data we promised.** MQ-135 (raw ADC, voltage, baseline, deviation), GPS (fix, satellites, lat/lon, altitude, UTC), uptime hours this Season, alive/stale indicator based on age of last reading. Recent-readings table for context.
- **11 dashboard tests + 6 oracle tests = 17 passing in 1.65s.** Tests stub out `tree_serial` and `oracle_client` so they run hermetically — no actual serial port, no actual oracle process needed.

### Failures / issues (encountered and resolved)

- **Pytest module-name collision.** First all-tests run failed: both components had `tests/test_basic.py` files, and pytest's default `prepend` import mode treats `tests/` as a top-level package — so it tried to import two modules at the same dotted path. **Fix:** rename to `test_oracle.py` and `test_dashboard.py`, remove the `__init__.py` files inside both `tests/` dirs, add a root `pyproject.toml` with `--import-mode=importlib` and explicit `testpaths`. Both unique test files now found, no conflict.

### Decisions

- **Dashboard talks to the oracle via HTTP only.** No shared DB, no shared in-process imports. Means the dashboard could run on a different host than the oracle later without any code changes.
- **One serial-open per command** in `tree_serial.py`, instead of a persistent connection. Slower per-call but stateless across Flask requests — and provisioning is a once-per-Tree operation, so the latency is invisible.
- **Polling (5s) instead of SSE/WebSocket** for live updates. Adds zero infrastructure, works through any reverse proxy, fine for the 60s sample cadence. Can upgrade later if cadence drops below polling interval.
- **Brand colors:** orange (`#ff8c42`, the JUICE accent) for primary actions, fresh green (`#8fde6e`) for OK/active states. Dark background for long sessions. No CSS framework — kept the entire stylesheet small enough to read in one pass (~150 lines).

### What's deferred

- **OTA upload UI.** Use `curl -F` against the Tree's `/ota` endpoint until Phase 4.1.
- **I2C bus scan / UART signature sniff** for sensor auto-detect — requires the dashboard to take over the serial port continuously, which doesn't fit the v1 one-shot-command model.
- **Multi-Tree admin actions** (delete, rename, force re-flash) — wait until there's a real fleet.
- **Orchard Pass NFT gate on `/register`** — that's Phase 6 (oracle-side check).

### What this unlocks

- **The full v1 loop is now demonstrable in real time.** Tree (firmware) → POST → oracle → SQLite, **with a browser tab open at `/tree/<node_id>` watching it happen**. That's the "close the loop where I can see it" milestone Richard asked for.

---

## 2026-05-27 — Phase 3: Oracle service

### Successes

- **Oracle service implemented end-to-end.** FastAPI 0.136 + SQLAlchemy 2.0 + Pydantic 2.12 + pydantic-settings. All on Richard's Python 3.14 install. Endpoints: `/`, `/health`, `/register`, `/readings` (POST + GET-by-node), `/nodes`, `/nodes/{id}`, `/uptime/{node}/{season}`.
- **Six smoke tests, all passing in 1.17s.** Covers: service identification, register (new + duplicate-same-key + duplicate-different-key conflict), POST with unknown node (404), POST with bad signature (401), POST happy path → retrieve → uptime bucket increment, uptime for unknown node (404).
- **HMAC-SHA256 verification matches the Tree firmware exactly.** Server reads raw bytes via `request.body()` (no JSON re-parsing before signature check, which would have broken the bytes), computes HMAC with stored signing key, constant-time compares with the `X-Orchard-Sig` header.
- **v1 Season math is in.** Day-aligned UTC Seasons starting from a configurable genesis date (default 2026-05-27). Phase 5 will swap `seasons.py` for Chia-block-aligned Seasons — the rest of the oracle treats `(node_id, season)` as opaque.
- **`oracle/data/` auto-creates on first run.** SQLite DB file lands at `oracle/data/orchard.db`. Directory is already covered by `.gitignore`.

### Failures / issues (encountered and resolved)

- **SQLite `:memory:` connections don't share state.** First test run failed with `no such table: nodes` even though `Base.metadata.create_all()` ran. Root cause: with `sqlite:///:memory:`, *each new connection gets its own empty database*. Schema was created on one connection, sessions used a different one. **Fix:** use `StaticPool` in the test engine so all sessions reuse a single connection. Production path is unaffected (uses file-backed SQLite).
- **Python 3.14 + new packages:** worth noting — fastapi 0.136, sqlalchemy 2.0.50, pydantic 2.12 all install cleanly on 3.14 without complaints. No version pinning gymnastics needed.

### Decisions

- **No NFT check on `/register` in v1.** Anyone with a Tree's `(node_id, signing_key_hex)` pair can register. Phase 6 will add Orchard Pass verification (operator must hold the credential NFT on the declared wallet). Documented in the oracle README.
- **Reading payload stored as raw JSON text + extracted GPS columns.** Full payload preserved for forensics + future-proofing; common fields (`gps_lat/lon/fix`, `fw_version`, `tree_ts_ms`) extracted into indexable columns for queries.
- **`/readings` returns 202 Accepted, not 200.** Semantically, the server has accepted the data and queued it for storage; the Tree doesn't need to wait on durability confirmation. Matters when we eventually add async persistence.

### What this unlocks

- **End-to-end data path is live.** Tree → signed POST → oracle SQLite → retrieve. Provision the existing Tree (COM4) over serial (`WIFI_SET`, `ORACLE_SET`) and the first real reading will flow.
- **Phase 4 (Orchard View)** can start — it has real endpoints to call.
- **Phase 5 (Season attestation writer)** can start — it has real uptime data to roll up.

---

## 2026-05-27 — First living Tree 🌱

**Tree node_id: `5B9BB022649FA93D4091DA4BA40714B9`** (ESP32-WROOM-32U in the prototype enclosure, on COM4).

### Successes

- **The Orchard is alive on real hardware.** Firmware flashed successfully (995,552 bytes, 10.6s upload at 752 kbit/s, hash verified). Chip rebooted into our firmware and produced the expected first-boot output.
- **First-boot identity generation worked end-to-end.** The two `nvs_get_blob NOT_FOUND` lines for `node_id` and `sign_key` are exactly what the firmware expects on a virgin chip — `identity::begin()` then generated fresh values and stored them in NVS. The Tree's `node_id` is now permanent: `5B9BB022649FA93D4091DA4BA40714B9`.
- **Sensor registry self-registration works.** Both `MQ135Sensor` and `GpsNeoSensor` AutoRegister<> instances pushed themselves into the registry at static-init time without any central wiring. Both passed `begin()` (`active=yes`) and the registry reports `2 active sensor(s)`.
- **WiFi manager and oracle client behave correctly on a virgin Tree.** WiFi: "no creds stored; idle. Use WIFI_SET over serial." Oracle: "WiFi not connected; skipping POST." Both are the right messages — no exceptions, no crashes, no silent failures.
- **Dual-target build proven viable.** Same source tree, two PlatformIO envs (`freenove_esp32_wroom` + `freenove_esp32s3`), one of each chip family will land on a Tree as the project grows.

### Failures / issues (encountered and resolved)

- **Auto-reset wasn't working on the WROOM-32U.** First upload failed with `Wrong boot mode detected (0x13)` — DTR/RTS dance via CP210x didn't bring the chip into download mode. **Workaround:** manual BOOT-button-held + RESET-tap + BOOT-still-held, run upload, release BOOT after writing starts. Worked first try with the manual procedure.
- **Wrong board assumption.** Memory and ADR initially recorded the prototype as ESP32-**S3**. Actually it's an ESP32-**WROOM-32U** (classic ESP32 with external antenna). Caught by esptool's chip-ID check before any bytes were written to the wrong chip. Memory + LOG corrected; platformio.ini now carries both envs with WROOM as the default.
- **Banner mangled in my capture script.** First-line output came back as `=== Themware ===` because my Python `serial.read(in_waiting or 1)` loop dropped bytes during the initial boot burst (latency between `in_waiting` checks). The firmware print is correct — verified with `pio device monitor` directly. Future captures should use a single `s.read(s.in_waiting)` after a brief sleep, or just use `pio device monitor` interactively.

### What this unlocks

- **Phase 3 (oracle) is now a real need, not a stub.** The Tree is generating data and signing it; nothing to send it to until the oracle exists.
- **Provisioning workflow is real.** Operators can already drive a Tree through `WIFI_SET`, `ORACLE_SET`, `STATUS`, `SAMPLE_NOW`, `REBOOT` via the serial console. Orchard View (Phase 4) will wrap this in a UI, but the underlying machinery is proven.

### Carry-over questions (parked, not blocking)

- *Auto-reset on the WROOM board — worth investigating the CP210x DTR/RTS timing? Or just document the manual BOOT-hold procedure as the standard for this board? (For now: documented procedure is fine.)*

---

## 2026-05-27 — First flash attempt: C++17 fix landed, wrong-chip detection saved us

### Successes

- **PlatformIO toolchain bootstrap.** Fresh install on Python 3.14. `python -m platformio` works (pio.exe ended up in `%APPDATA%\Python\Python314\Scripts`, not on PATH — fine, we just use the `python -m` form). Espressif32 platform 6.13.0 + xtensa toolchain + Arduino-ESP32 v3.20017 framework downloaded and cached (~300MB, one-time).
- **First compile uncovered a real bug.** sensor.h used `std::make_unique` (C++14+) but Arduino-ESP32 v2.x defaults to gnu++11. Fix: `build_unflags = -std=gnu++11` + `build_flags = -std=gnu++17` in `platformio.ini`. After the change, full clean build succeeds — every translation unit compiles, archive links, firmware.bin gets generated. **RAM 14.5% used, Flash 30.3% used** — lots of room for more sensors and features.
- **esptool's chip-ID guard worked exactly as designed.** Caught the wrong-target situation before writing a single byte to flash. Zero bricking risk on the wrong-board attempt.

### Failures / issues

- **`std::make_unique` C++14 dependency.** Already fixed (see above). Lesson: when writing firmware that uses standard-library features, explicitly set the language standard in `platformio.ini` rather than relying on the framework's defaults — Arduino-ESP32 v2.x is conservative.
- **Wrong board on COM6.** esptool refused: `This chip is ESP32, not ESP32-S3`. The board on COM6 (Silicon Labs CP210x bridge) is a **classic ESP32**, not the Freenove ESP32-S3 we built firmware for. Our target is the S3 — the one in the prototype enclosure, using a CH343 bridge.
- **USB driver state was confusing.** WCH CH343 driver showed `Status: Unknown` initially even with `wch.cn / ch343ser.inf` registered as a system driver. Resolution wasn't a driver reinstall — it was unplug + replug the board to force re-enumeration. CH343 board has shown up at COM17 in one session and not at all in others, suggesting an intermittent USB connection (cable or socket).

### Decisions

- **Target C++17 for the firmware permanently.** Modern features (`std::make_unique`, `std::optional`, structured bindings, `if constexpr`) are worth the small compile-time overhead. Documented in `platformio.ini` comments.
- **No build env for classic ESP32 yet.** The firmware leans on USB-CDC-on-boot and S3-only behaviors. Adding a classic-ESP32 env is real work (pin remapping, no native USB) and we don't need it for v1. If a contributor wants to support classic ESP32 later, they add `[env:esp32_classic]` and gate the S3-specific bits with `#if CONFIG_IDF_TARGET_ESP32S3`. Filing as a possible v1.x add.

### Carry-over questions (parked, not blocking)

- *What's on the classic ESP32 (COM6)? Project / use case for it? Worth supporting as a non-S3 Tree variant later?*
- *CH343 enumeration intermittency — bad cable, flaky USB-C socket, or PC USB port? Try a known-good cable first; if still flaky, the board's USB-C socket may need reflowing.*

---

## 2026-05-27 — GitHub repo renamed `DeMeterData` → `the-orchard`

### Successes

- **Repo renamed in GitHub Settings** by Richard. New canonical URL: https://github.com/FlipThisCrypto/the-orchard. GitHub auto-redirects the old `DeMeterData` URL, so any existing clones, badges, or external links keep resolving.
- **Local `origin` remote updated** to the new URL via `git remote set-url`. `git fetch origin` works against the new name. No history rewrite needed.
- **Doc references swept.** README quickstart, CONTRIBUTING.md, ADR-0001, and the `project_orchard_overview` memory file now use the new URL.

### Notes

- **Earlier LOG entries are deliberately not rewritten.** When the kickoff entry says "Public GitHub at .../DeMeterData", that's the truth as of that timestamp. Editing history breaks the value of the LOG.
- **`origin/HEAD`** on the new repo still points at `main` — no remote-side cleanup needed.

---

## 2026-05-27 — Phase 2: Tree firmware (initial)

### Successes

- **Phase 2 firmware committed.** PlatformIO + Arduino-ESP32 framework, targeting `esp32-s3-devkitc-1` (closest PlatformIO board match for Freenove ESP32-S3 — pin mappings line up).
- **Modular sensor architecture working as designed.** `SensorRegistry` + static `AutoRegister<>` instances mean each driver is one `.cpp` + one registration line, no central if-else. Two active drivers landed: MQ-135 (analog air quality) and GPS NEO (UART NMEA, parsed by TinyGPSPlus).
- **Identity layer.** Per-Tree `node_id` (16 random bytes, hex-encoded) and 32-byte signing secret generated on first boot, persisted to NVS via the `Preferences` library, never transmitted over the network. Secret only ever leaves the device over the local USB-serial console (via the `KEY` command) during registration.
- **Signed POSTs.** Each `/readings` request carries `X-Orchard-Sig: HMAC-SHA256(secret, body)` using `mbedtls/md.h` (no extra crypto library).
- **Serial-console provisioning.** Line-oriented commands (`PING`, `STATUS`, `NODE_ID`, `KEY`, `WIFI_SET`, `WIFI_CLEAR`, `ORACLE_SET`, `SAMPLE_NOW`, `REBOOT`) — Orchard View (Phase 4) will drive these; meanwhile they work fine from `pio device monitor`.
- **HTTP OTA on `/ota`** + a `/health` endpoint. Dashboard pushes new firmware via a multipart POST. No auth on `/ota` — explicitly LAN-only.
- **Partition table** for 8MB flash with two 3MB OTA app slots + NVS + small SPIFFS. 4MB-flash users swap to `default.csv`.

### Decisions

- **v1 signing = HMAC-SHA256, not ed25519.** Saves ~25KB flash + one library dependency. The oracle is the v1 trust boundary per ADR-0001, so the asymmetric-key step gives v1 no security benefit. The `identity::sign(...)` interface hides the scheme so v2 can swap to ed25519 (or whatever) without touching drivers. Recorded in `firmware/README.md` and `firmware/src/identity.h`.
- **Stub drivers for AHT20 / BMP280 / BH1750 / PMS5003 deliberately not included.** No sensor wired = no honest way to test. Contributors use the existing `examples/sensor_driver/template` to add them when their hardware arrives.
- **GPS on UART1** (GPIO 4/5, baud 9600). PMS5003 reserved for UART2 (GPIO 16/17). Matches the working setup.

### Failures / open issues

- *(none yet — firmware has not been flashed to a real Tree as of this commit. First flash + bring-up will go in the next LOG entry.)*

### Carry-over questions (parked, not blocking)

- *Verify Freenove ESP32-S3 flash size (4MB vs 8MB). Default in `platformio.ini` is 8MB; adjust if the board is 4MB.*
- *Confirm GPS UART pin mapping against the actual board wiring (GPIO 4=RX, GPIO 5=TX). If the GPS already worked with the previous firmware, those are right.*

---

## 2026-05-27 — $JUICE token details + sensitive-data hygiene

### Successes

- **Read `docs/Juice Token.docx`** and extracted the full $JUICE token reference. Public-safe summary now lives at [`docs/token/JUICE.md`](token/JUICE.md). README "The token" section updated with logo, supply, Eve Coin ID.
- **Confirmed token economics anchors:**
  - Total supply: **100,000,000 JUICE** (single issuance).
  - Type: Chia CAT, mainnet.
  - Eve Coin ID: `2ff338ed6fb3161d48eed7f112d3c6077e90c517dc4534bfba8ad3975b7f5e63`.
- **Logo files added:** `docs/photos/logo.png` (transparent bg), `docs/photos/logo1.png` (dark bg), `docs/photos/Juice logo small.png`. Transparent variant wired into the README.

### Security actions taken

- **Added `docs/Juice Token.docx` to `.gitignore`.** The docx contains operator-private wallet info (wallet fingerprint, wallet id, wallet label) that should never land in a public repo. Glob patterns also catch any future `*token*.docx` and `docs/token/*-private.*`.
- **Stored operator-private $JUICE details in memory only** (`project_token_juice_private.md`). Not in the repo, not in any committed file.
- **Convention established:** `config.example.yaml` will use placeholders (`fingerprint: 0`); the real values go in `config.yaml` (gitignored).

### Failures / open issues

- *(none new this entry)*

### Lessons

- **Always scan dropped files for operator-private data before integration.** A token-creation log is the kind of file that *looks* like project docs but contains exactly the secret an attacker would want.

---

## 2026-05-27 — Vision locked, formal naming applied

### Successes

- **Vision document authored.** Captured in [VISION.md](VISION.md) and in memory at `project_vision.md`. Layered architecture (Hardware / Identity / Data / Rewards), brand naming table, long-term reward-logic factors (sensor diversity, geographic scarcity, validated submissions, reputation, Orchard Pass tier bonuses), and tokenomics direction ($JUICE fixed supply, small experimental LP first, Orchard Passes carry later utility).
- **Naming finalized — authoritative table:** Ecosystem = The Orchard, Token = $JUICE, Nodes = **Trees**, Node Clusters = **Groves**, Reward Cycles = **Seasons**, Data Collection = **Harvest**, Validators = **Keepers**, Dashboard = **Orchard View**, Sensor NFTs = **Orchard Passes**.
- **Cascade applied:** README rewritten with Glossary section + vision teaser; ADR-0001 updated (Season, Orchard Pass, $JUICE, Tree); all module READMEs updated; tasks #4-#7 renamed; memory files synced. Earlier "sapling" proposal removed everywhere (only historical mentions remain in this LOG below).
- **Resolved carryover questions:** $JUICE Asset ID (`285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121`); antenna is WiFi/BT + corded GPS (no LoRa).

### Decisions

- **Code vs brand language separation.** User-facing copy (READMEs, dashboard UI, marketing) uses Trees / Orchard Passes / Seasons. Code (variables, JSON keys, DB tables) uses `node` / `pass` / `season`. README has a Glossary mapping them.
- **Reserved Orchard Pass attributes** (`Tier`, `Reward Multiplier`, `Sensor Manifest`, `Geographic Region`, `Reputation Score`) documented in `nft/README.md` so v1 mints stay forward-compatible with vision-era features without breaking collection metadata.

### Carry-over questions (parked, not blocking)

- *GitHub repo rename `DeMeterData` → `the-orchard` (or similar)? Cosmetic, GitHub auto-redirects old URL.*
- *Local working folder rename `I:\DeMeter Data\Chia DePIN` → something Orchard-themed? Cosmetic.*
- *Commit + push approval — still pending.*

---

## 2026-05-27 — Project rename + new facts in

### Successes

- **Project renamed:** "DeMeter Data" → **"The Orchard"**. Cascaded through README, LICENSE, CONTRIBUTING, ADR-0001, and all module READMEs. Token name **$JUICE** integrated. Memory and ADR document the original name so the history isn't lost.
- **Token confirmed:** $JUICE on Chia mainnet, asset id `285164e6af80202d2b07fa3cc6ae47ff2906029365a83c50fcab25a56b937121`. Now lives in the README + docs; will live in `chia/config.example.yaml` once Phase 5 starts.
- **Antenna identification:** the larger ~4" antenna in the prototype photo is a **WiFi/Bluetooth** antenna; the corded one is the GPS antenna. No LoRa module installed.
- **Naming theme established:** project = The Orchard, token = $JUICE, nodes = "saplings" (proposed; awaiting Richard's sign-off), NFT collection = "The Orchard — Genesis Saplings" (proposed).

### Failures / open issues

- *(none new this entry)*

### Decisions

- **No LoRa pin reservation in v1.** Confirmed no LoRa module exists yet. Firmware's modular radio interface keeps LoRa addable later without reserving pins now. PMS5003 gets UART2 instead of fighting for it.
- **Sapling naming.** Proposed convention: NFT credentials are called "saplings", deployed nodes are called "trees" (mature saplings). Token harvested = $JUICE. Pending Richard's final approval.

### Carry-over questions (parked, not blocking)

- *Confirm sapling/tree naming, or pick alternative.*
- *Rename local working folder `I:\DeMeter Data\Chia DePIN` → something Orchard-themed? (Cosmetic, can wait.)*
- *Rename GitHub repo `DeMeterData` → `the-orchard` (or similar)? Old URL auto-redirects via GitHub.*

---

## 2026-05-27 — Project kickoff

### Successes

- **Hardware bring-up:** Freenove ESP32-S3 in clear enclosure, GPS (NEO series) + MQ-135 wired and powered. GPS is producing valid NMEA ($GPGSV, $GPGLL, $GPRMC) with a stable fix in Mount Washington, KY. MQ-135 readings observed.
- **Architecture decisions locked.** See [decisions/0001-v1-architecture.md](decisions/0001-v1-architecture.md).
- **Repo created.** Public GitHub at https://github.com/FlipThisCrypto/DeMeterData.
- **License chosen:** Apache 2.0 (patent grant matters for an infrastructure project).
- **Chia infra confirmed running:** full node + DataLayer service active on the same dev PC as the dashboard — RPCs available on localhost.
- **CAT token already minted** on Chia mainnet, with tokens in Richard's wallet ready for first payouts.

### Failures / open issues

- **Previous oracle was returning 500/422.** The old backend was repurposed from another project, so its schema didn't match what the ESP32 was sending. Confirmed dead — being rebuilt from scratch in Phase 3. Lesson: a small purpose-built service beats a misaligned reuse.
- **BH1750 light sensor logged "Device is not configured!"** — root cause is that it isn't wired yet, not a software bug. Will resolve when sensor is physically connected (Phase 2 wiring docs).
- **No automatic detection for analog sensors.** Confirmed that I2C devices can be enumerated (bus scan + address-to-type lookup) and UART devices can be sniffed (NMEA / PMS5003 signature detection), but analog sensors like MQ-135 require user declaration. Dashboard will include a sensor-declaration form.

### Decisions

- **v1 deployment scope:** 1–5 mains-powered WiFi nodes near Richard. Firmware kept modular for future LoRa / battery / off-grid scenarios.
- **Reward distribution v1:** manual batched CAT spend bundle from Richard's wallet, no Chialisp claim contract yet.
- **DataLayer scope v1:** daily uptime attestations only. Raw sensor data stays in the local oracle SQLite DB.
- **NFT credential:** new collection to be designed and minted. One NFT per wallet, enforced at registration time.

## 2026-07-21 � 50-iteration DataLayer hardening pass

Sequential improvements: dashboard oracle resilience, preflight/reconcile CLIs, closed-hour publish (earlier), ops journals, beacon cache, post-write confirm, RPC retry, operator runbook, exit codes, verify --json, and related tests. Branch: datalayer-verifiable-dataset.

