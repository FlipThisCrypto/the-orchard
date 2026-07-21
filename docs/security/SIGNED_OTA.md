<!-- SPDX-License-Identifier: Apache-2.0 -->
# T7 — Signed OTA firmware updates (owner hand-off)

> **Status:** design + procedure ready; **two steps are owner-gated** and are
> called out inline as **`OWNER:`**. Everything else (the firmware verifier and
> the CI signing step) is normal engineering that can land once the release
> keypair exists. Nothing here has been executed — no key has been generated and
> no secret has been added.

## Goal & threat model

The Tree exposes an OTA endpoint (`POST /ota` on port 80, see
`firmware/src/net/ota.cpp`). Today an update is gated only by the local
**`OTA_ARM`** serial command, which opens a short upload window. That stops a
*remote, unattended* push, but it does **not** prove the image is authentic: a
hostile actor on the same LAN, during the arm window, can flash arbitrary
firmware. v0.4.8 ships intentionally unsigned.

T7 adds **image authenticity**: a Tree only applies an OTA image that carries a
valid signature from the project's release key. This protects against a
malicious or corrupted image being flashed during the arm window, and is a
prerequisite for ever allowing a less-manual OTA path.

> Scope note: this is **app-level OTA signing**, verified by the running
> firmware. It is independent of, and complementary to, ESP-IDF **Secure Boot**
> (hardware root of trust, eFuse-based) — that lives in the T10 hand-off
> ([FLASH_ENCRYPTION.md](FLASH_ENCRYPTION.md)). Signed OTA needs no eFuse burn
> and is fully reversible, so it should land first.

## Design

Reuse the primitive the device already has. Per
[ADR-0007](../decisions/0007-secp256r1-device-keys.md) every Tree already does
secp256r1 (P-256) ECDSA over SHA-256 with bundled mbedTLS. We sign **releases**
with the same curve, so the verifier on-device is a few lines against code
that's already linked in.

- **Release keypair:** one P-256 keypair for the whole fleet. The **private**
  key signs each released image and lives **only** in CI as an encrypted secret.
  The **public** key is compiled into the firmware (`firmware/include/`), so
  every Tree can verify but none can sign.
- **Signature:** detached. The release workflow computes
  `ECDSA_P256( SHA-256( image_bytes ) )`, low-S normalized, 64-byte `r‖s`
  (identical encoding to the device-reading signatures in
  `orchard_chia/datalayer/SPEC.md`), and uploads it next to the image as
  `<asset>.sig` (and records it in `SHA256SUMS.txt`).
- **Verification:** the `/ota` handler streams the upload through a running
  SHA-256, and before `Update.end()` commits, checks the supplied signature
  against the baked-in release public key. On mismatch it aborts the `Update`
  and returns `403`.

### Why not the Arduino `Update` built-in signature check?

Arduino-ESP32's `Update` can verify an appended RSA/ECDSA signature, but it
pulls in a second signing toolchain and key format. We already ship a P-256
verifier and a canonical 64-byte signature convention; reusing them keeps one
key format, one curve, and one mental model across device readings, DataLayer
records, and OTA. If the built-in path is later preferred, the rollout below
still applies.

## Staged rollout (don't brick OTA)

Enforcing signatures on a Tree that doesn't yet know the public key would reject
**every** image, including the fix. Roll out in order:

1. **Ship the verifier in "warn" mode.** A firmware release that contains the
   release public key and *computes* the verdict but, when
   `ota_require_signature` (NVS flag, default **false**) is off, only logs
   `[ota] signature INVALID (warn mode)` and still flashes. Now the fleet knows
   the key.
2. **Sign releases.** Turn on the CI signing step (below) so every new
   `*-web-*.bin` / OTA `*.bin` ships with a `.sig`.
3. **Flip enforcement.** Once every Tree runs a warn-mode build *and* releases
   are signed, set `ota_require_signature=true` (serial console command, same
   pattern as `require_seq`). From then on an unsigned/mis-signed image is
   refused. This mirrors the T3 `require_seq` rollout exactly.

## OWNER actions

**`OWNER:` 1 — generate the release keypair (offline, once).**
```bash
# Private key — NEVER commit this; it goes into CI as a secret (step 2).
openssl ecparam -name prime256v1 -genkey -noout -out orchard-ota-release.key
# Public key, raw uncompressed point -> compressed SEC1 (what the device bakes).
openssl ec -in orchard-ota-release.key -pubout -conv_form compressed \
    -outform DER 2>/dev/null | tail -c 33 | xxd -p -c 33
```
Keep `orchard-ota-release.key` in a password manager / offline vault. If it
leaks, anyone can sign firmware for the whole fleet — rotate by shipping a new
warn-mode build with the new public key, then re-enforcing.

**`OWNER:` 2 — add the private key as a CI secret.**
In the GitHub repo: *Settings → Secrets and variables → Actions → New
repository secret*, name **`OTA_SIGNING_KEY`**, value = the full contents of
`orchard-ota-release.key`. That is the only place the private key lives online.

Hand the compressed-pubkey hex from step 1 to the firmware change (it gets
baked into `firmware/include/` as `ORCHARD_OTA_RELEASE_PUBKEY`). Publishing a
*public* key in the repo is expected and safe.

## CI signing step (engineering, after OWNER step 2)

**Landed** in `.github/workflows/release.yml` + `tools/sign_release.py`:

- Loads the PEM from env `OTA_SIGNING_KEY` (never a committed file).
- Emits `<image>.bin.sig` as 64-byte `r‖s` low-S over SHA-256(image).
- Appends `.sig` digests to `SHA256SUMS.txt`.
- If the secret is **missing**, prints `UNSIGNED` and exits 0 so forks and
  pre-OWNER tags still publish (warn-mode rollout). Use `--require` only if you
  want a hard fail for a production cut.

Bake the compressed public key from OWNER step 1 into firmware as
`ORCHARD_OTA_RELEASE_PUBKEY_HEX` in `firmware/include/ota_release_pubkey.h`
(or via a `-D` build flag). The private key must **never** enter the repo.

## Firmware implementation (status)

| Piece | Status |
|-------|--------|
| `firmware/include/ota_release_pubkey.h` | Landed — empty default (no key baked) |
| Streaming SHA-256 of OTA upload | Landed in `firmware/src/net/ota.cpp` |
| `X-Orchard-Ota-Sig` header (128 hex chars) | Landed |
| Verify against release pubkey | Landed (mbedTLS ECDSA) |
| Warn mode when invalid/missing | Landed (default) |
| NVS `ota_require_signature` + `OTA_REQUIRE_SIG 0\|1` | Landed |
| `identity::p256_verify` helper | Landed |
| Release pubkey filled in | **OWNER** after keygen |
| Dashboard OTA push sends `.sig` | Optional follow-up |

Until the pubkey hex is non-empty, devices log that signature check is skipped.

## Verifying it works

1. Warn-mode build flashed; push a **correctly** signed image → log shows
   `signature OK`, flash succeeds.
2. Push a **tampered** image (flip a byte) with the old sig → warn mode logs
   `INVALID (warn)` and still flashes; with `ota_require_signature=true` it is
   refused with `403` and the running firmware is untouched.
3. Confirm a Tree that never received the pubkey is *not* in the field before
   enforcement is flipped (it would reject all OTAs).

## References

- ADR-0007 — secp256r1 device keys (the reused primitive).
- Espressif, *Over The Air Updates* and *Secure OTA* guides (authoritative for
  the `Update` library + secure-boot interaction).
- T10 hand-off — [FLASH_ENCRYPTION.md](FLASH_ENCRYPTION.md) (hardware root of
  trust; do that *after* signed OTA).
