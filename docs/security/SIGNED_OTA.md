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

In `.github/workflows/release.yml`, after the images are built and before they're
uploaded, add a sign step that runs only when the secret is present:

```yaml
      - name: Sign OTA images
        if: ${{ env.OTA_SIGNING_KEY != '' }}
        env:
          OTA_SIGNING_KEY: ${{ secrets.OTA_SIGNING_KEY }}
        run: python tools/sign_release.py --key-env OTA_SIGNING_KEY dist/*.bin
```

`tools/sign_release.py` (follow-up PR) loads the key from the env var (never a
file path in the repo), emits `<image>.sig` as 64-byte `r‖s` low-S, and appends
the pair to `SHA256SUMS.txt`. Gating on `OTA_SIGNING_KEY != ''` means forks and
pre-secret builds still succeed (just unsigned), matching the warn-mode rollout.

## Firmware implementation (follow-up PR scope)

- `firmware/include/` — add `ORCHARD_OTA_RELEASE_PUBKEY` (compressed hex from
  OWNER step 1).
- `firmware/src/net/ota.cpp` — hash the streamed upload; accept the detached
  signature (multipart field or `X-Orchard-Ota-Sig` header); verify with the
  existing P-256 verify path (mirror `identity::p256_sign`'s counterpart) before
  `Update.end()` commits; abort + `403` on mismatch.
- NVS flag `ota_require_signature` (default false) + a serial console command to
  flip it, mirroring `require_seq`.
- The dashboard's OTA push UI sends the `.sig` alongside the image.

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
