<!-- SPDX-License-Identifier: Apache-2.0 -->
# T10 — Flash encryption & NVS-at-rest (owner hand-off)

> **Status:** procedure + decision framing ready. **Every enabling step is
> owner-gated and IRREVERSIBLE** — they burn eFuses. Nothing here has been run.
> Do **not** execute any command in this document against a Tree you care about
> until you have read it in full and rehearsed on a **sacrificial board**.

> ⚠️ **Irreversibility.** eFuse bits are one-way. Enabling Flash Encryption or
> Secure Boot in **Release mode** cannot be undone, cannot be downgraded to
> plaintext, and a mistake can **permanently brick the board**. Treat this like
> blowing a fuse, because it is one.

## What we're protecting

Each Tree persists secrets in the `orchard` NVS namespace
(`firmware/src/identity.cpp`):

- the **secp256r1 private key** (the device's signing identity, ADR-0007),
- the legacy **HMAC signing secret**,
- the **claim nonce** (ADR-0005), and the soft-AP password.

By default these sit in **plaintext** in the SPI flash. Anyone with brief
*physical* access and a flash reader (or the UART download mode) can dump them,
clone the device's identity, and forge signed readings from that node.

T10 closes that physical-readout gap with two independent eFuse-based features:

| Feature | Protects | eFuse | Reversible |
|---|---|---|---|
| **Flash Encryption** | confidentiality of flash contents (app, and NVS *if* NVS encryption is also enabled) | yes | **no** (Release mode) |
| **Secure Boot v2** | integrity — only a signed bootloader/app will run | yes | **no** |
| **NVS Encryption** | the NVS partition specifically (needs Flash Encryption on) | (uses keys partition) | n/a |

Flash Encryption alone does **not** encrypt the `nvs` partition unless **NVS
Encryption** is also configured — and that's exactly where our device key lives.
So the device-key-at-rest goal requires **Flash Encryption + NVS Encryption**.
Secure Boot is the natural pairing (and is the hardware counterpart to the
app-level [signed OTA](SIGNED_OTA.md)), but is a separable decision.

## Hard constraint: Arduino-on-PlatformIO doesn't expose this cleanly

Our firmware builds as **Arduino framework under PlatformIO**
(`firmware/platformio.ini`). Flash Encryption / Secure Boot / NVS Encryption are
configured through ESP-IDF `sdkconfig` options and a custom bootloader — which
the prebuilt Arduino-PlatformIO toolchain does **not** surface. Realistically
this means one of:

1. **Arduino-as-an-IDF-component build** (`idf.py` with Arduino pulled in as a
   component), where `sdkconfig` controls `CONFIG_SECURE_FLASH_ENC_ENABLED`,
   `CONFIG_NVS_ENCRYPTION`, `CONFIG_SECURE_BOOT`, etc. This is the supported,
   documented path but is a build-system change.
2. **Post-build eFuse + re-flash with `espefuse.py` / `esptool.py`** using
   images produced by an IDF-component build.

Either way, **T10 is gated on moving the secure build to an ESP-IDF-component
pipeline.** That work (a spike + a parallel build target) should be scoped
before any eFuse is burned. Do **not** attempt to bolt encryption onto the
stock Arduino-PlatformIO artifacts — the bootloader won't match and you'll brick
boards.

## Chip differences (we ship both)

The fleet has **ESP32-WROOM-32** (classic) and **ESP32-S3** boards. Flash
Encryption and Secure Boot v2 exist on both, but **the eFuse layout, key
blocks, and `espefuse.py` subcommands differ between ESP32 and ESP32-S3.** Run
the chip-appropriate procedure; never copy an ESP32 eFuse recipe onto an S3 (or
vice-versa). `espefuse.py --chip esp32` vs `--chip esp32s3` summaries are the
source of truth for each board in hand.

## Procedure (owner, on a sacrificial board first)

This is the *shape* of the work, not a substitute for the official Espressif
guides — follow those for the exact, chip-specific commands, because they are
versioned to your IDF release and the wrong bit is unrecoverable.

1. **Stand up the IDF-component build** (see constraint above) and confirm a
   *plaintext* image boots normally on a throwaway board.
2. **Development-mode Flash Encryption first.** Enable Flash Encryption in
   **Development** mode (`CONFIG_SECURE_FLASH_ENCRYPTION_MODE_DEVELOPMENT`).
   Dev mode still lets you re-flash a limited number of times, so you can
   iterate. Verify the device boots, generates its identity, posts a reading.
   **Dev mode is not secure** (encryption can be disabled via eFuse) — it exists
   only to rehearse.
3. **Add NVS Encryption.** Configure `CONFIG_NVS_ENCRYPTION` + an `nvs_keys`
   partition so the `orchard` namespace is encrypted. Re-verify identity
   persistence across reboots (a botched keys partition = the device can't read
   its own key back).
4. **Add Secure Boot v2** (optional but recommended): generate the secure-boot
   signing key (keep it offline, like the OTA key), enable
   `CONFIG_SECURE_BOOT`. Verify signed bootloader + app boot.
5. **Only once 2–4 pass on sacrificial hardware:** burn **Release** mode on a
   real Tree. This disables UART access to flash and makes the above permanent.

After Release-mode burn: the board can no longer be read out or downgraded; OTA
(signed, T7) becomes the normal update path; a lost secure-boot key means you
can never ship that fleet a new bootloader.

## Recommendation / decision for the owner

T10 defends against an attacker with **physical possession** of a Tree who wants
to extract its key and impersonate the node. For the v1 tester phase, weigh:

- **Impact if skipped:** a stolen/borrowed Tree's identity can be cloned; that
  node's readings could be forged. Mitigations already in place: per-device keys
  (one compromise ≠ fleet compromise), the claim/Pass binding (ADR-0005), and
  small early rewards.
- **Cost to do now:** the IDF-component build migration + irreversible burns on
  a mixed ESP32/ESP32-S3 fleet, with real brick risk.

A reasonable call is to **ship signed OTA (T7) for the tester gate and defer the
eFuse burns (T10) until rewards are non-trivial or units ship to untrusted
hands** — i.e. T10 is "harden before scale," not "blocks first testers." This
doc exists so the burn can be done confidently when that line is crossed. The
dashboard currently lists T10 as tester-blocking; this is the input to revise
that if you agree.

## References

- Espressif, *Flash Encryption* (per-chip: ESP32, ESP32-S3) — **authoritative**.
- Espressif, *Secure Boot v2* (per-chip).
- Espressif, *NVS Encryption* + `nvs_keys` partition.
- `espefuse.py` / `esptool.py` docs for the exact eFuse summary + burn commands.
- [SIGNED_OTA.md](SIGNED_OTA.md) — do T7 first; it's reversible and unblocks the
  update path that Release-mode encryption relies on.
