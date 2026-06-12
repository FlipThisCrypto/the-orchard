// SPDX-License-Identifier: Apache-2.0
//
// Per-Tree identity: a stable node_id, a legacy HMAC secret, and a
// secp256r1 (P-256) keypair — all persisted to NVS on first boot.
//
// Legacy signing (kept): HMAC-SHA256 with a 32-byte secret shared with the
// oracle at provisioning time.
//
// ADR-0003/ADR-0007 signing: secp256r1 ECDSA over sha256, RFC 6979
// deterministic (mbedTLS, bundled — no extra library). The device signs
// each reading with a private key it never transmits; the PUBLIC key is
// exported (HW_INFO / PUBKEY) and published in the node:<id> DataLayer
// record, so anyone can verify a reading's provenance without trusting
// the oracle — including a CLVM puzzle via `secp256r1_verify` (ADR-0008).
// The canonical message format the device must sign is pinned in
// docs/datalayer/SPEC.md §2.3 + orchard_chia/datalayer/testdata/vectors.json
// ("reading_canonical").

#pragma once

#include <Arduino.h>
#include <cstdint>

namespace orchard::identity {

// Initialize / load identity from NVS. Generates a fresh node_id and
// signing secret on first boot. Idempotent.
void begin();

// Hex-encoded node id (16 bytes -> 32 hex chars).
const String& node_id_hex();

// Raw 32-byte HMAC secret. Used by the oracle client to sign payloads.
// DO NOT print this casually — it's the device's private key.
const uint8_t* signing_secret();
constexpr size_t kSigningSecretLen = 32;

// Compute HMAC-SHA256 over `data` using the device signing secret.
// `out` must be 32 bytes.
void hmac_sha256(const uint8_t* data, size_t len, uint8_t out[32]);

// Monotonically increasing sequence number, persisted to NVS.
// Survives reboots and crashes (reservation-block scheme: NVS is
// written once per kSeqReserve calls, and on boot the counter resumes
// from the persisted watermark — possibly skipping numbers, never
// repeating one). Include in every signed payload; the oracle rejects
// any submission whose seq is not strictly greater than the last
// accepted one, which kills replay attacks. (And per ADR-0008, this
// counter is the ancestor of the on-chain heartbeat counter.)
uint32_t next_seq();

// --- secp256r1 (P-256) device key (ADR-0003 + ADR-0007) ----------------
constexpr size_t kP256PrivLen = 32;  // raw private scalar, big-endian
constexpr size_t kP256PubLen  = 33;  // compressed SEC1 point
constexpr size_t kP256SigLen  = 64;  // r||s, two 32-byte big-endian ints

// Lowercase-hex compressed SEC1 public key (66 hex chars). Published in
// the node:<id> DataLayer record; verifiers check each reading against it.
// Lowercase to match the schema's hex convention (SPEC §0).
const String& p256_pubkey_hex();

// Sign sha256(`data`) with the device's P-256 key: RFC 6979 deterministic
// ECDSA, low-S normalized, written to `out` as 64-byte r||s (SPEC §4 —
// the exact form CLVM's `secp256r1_verify` consumes). The caller
// hex-encodes (lowercase) into the reading's `sig`. On internal failure
// `out` is zeroed (an all-zero sig never verifies).
void p256_sign(const uint8_t* data, size_t len, uint8_t out[kP256SigLen]);

// Hex-encode a buffer, LOWERCASE (no separators). For sig/pubkey hex,
// which the schema requires lowercase.
String to_hex_lower(const uint8_t* buf, size_t len);

// Soft-AP password for the WiFi provisioning fallback.
//
// Generated on first call (random ORCHARD_AP_PASSWORD_LEN chars from a
// printable alphabet), persisted in NVS, and stable across reboots.
// The first generation prints the password ONCE to the serial console
// so the operator can record it. Returned as a String for direct use
// with WiFi.softAP(ssid, password).
//
// Subsequent calls do NOT re-print the password — recovering a lost
// AP password is an explicit operator action (NVS wipe + reboot) so
// you don't accidentally leak it by tailing the boot log.
const String& ap_password();

// Hex-encode a buffer into a String (uppercase, no separators).
String to_hex(const uint8_t* buf, size_t len);

}  // namespace orchard::identity
