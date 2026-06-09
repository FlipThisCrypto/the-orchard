// SPDX-License-Identifier: Apache-2.0
#include "identity.h"

#include <Ed25519.h>
#include <Preferences.h>
#include <bootloader_random.h>
#include <esp_random.h>
#include <mbedtls/md.h>

#include "config.h"

namespace orchard::identity {

namespace {

constexpr size_t kNodeIdBytes = 16;
constexpr const char* kNvsKeyNodeId  = "node_id";
constexpr const char* kNvsKeySecret  = "sign_key";
constexpr const char* kNvsKeyEdSeed  = "ed_seed";
constexpr const char* kNvsKeyAPPw    = "ap_pw";

// Printable alphabet for AP passwords. Skips characters that are
// hard to read or hard to type on a phone:  0 O o 1 l I.
constexpr const char* kAPPwAlphabet =
    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
constexpr size_t kAPPwAlphabetLen = 56;

uint8_t signing_secret_[kSigningSecretLen] = {0};
uint8_t ed_seed_[kEd25519PubLen] = {0};   // 32-byte ed25519 private seed
uint8_t ed_pub_[kEd25519PubLen] = {0};    // derived public key
String node_id_hex_;
String ed_pubkey_hex_;
String ap_password_;

void random_bytes(uint8_t* out, size_t len) {
  // esp_random() is hardware-backed when WiFi/BT is active. Before that,
  // it falls back to a deterministic PRNG. Mixing in micros() helps the
  // very-first-boot case.
  for (size_t i = 0; i < len; i += 4) {
    const uint32_t r = esp_random() ^ static_cast<uint32_t>(micros());
    const size_t chunk = (len - i >= 4) ? 4 : (len - i);
    memcpy(out + i, &r, chunk);
  }
}

String generate_ap_password(size_t len) {
  // Random per-device printable secret. Each character is drawn from
  // kAPPwAlphabet using esp_random() % alphabet_len. The modulo bias
  // is irrelevant for an alphabet size of 56 — the bias toward the
  // first few characters is < 0.5% per char.
  String s;
  s.reserve(len);
  for (size_t i = 0; i < len; ++i) {
    const uint32_t r = esp_random() ^ static_cast<uint32_t>(micros());
    s += kAPPwAlphabet[r % kAPPwAlphabetLen];
  }
  return s;
}

}  // namespace

String to_hex(const uint8_t* buf, size_t len) {
  static const char* kHex = "0123456789ABCDEF";
  String s;
  s.reserve(len * 2);
  for (size_t i = 0; i < len; ++i) {
    s += kHex[(buf[i] >> 4) & 0x0f];
    s += kHex[buf[i] & 0x0f];
  }
  return s;
}

String to_hex_lower(const uint8_t* buf, size_t len) {
  static const char* kHex = "0123456789abcdef";
  String s;
  s.reserve(len * 2);
  for (size_t i = 0; i < len; ++i) {
    s += kHex[(buf[i] >> 4) & 0x0f];
    s += kHex[buf[i] & 0x0f];
  }
  return s;
}

void begin() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);

  // Read whatever identity already exists. getBytes() both loads the
  // value AND tells us (via the returned length) whether it was present,
  // so a single call per key decides "do we need to generate this".
  uint8_t node_id_buf[kNodeIdBytes] = {0};
  const bool need_node =
      prefs.getBytes(kNvsKeyNodeId, node_id_buf, kNodeIdBytes) != kNodeIdBytes;
  const bool need_secret =
      prefs.getBytes(kNvsKeySecret, signing_secret_, kSigningSecretLen)
          != kSigningSecretLen;
  const bool need_ed =
      prefs.getBytes(kNvsKeyEdSeed, ed_seed_, sizeof(ed_seed_))
          != sizeof(ed_seed_);

  // H5 hardening: persistent device keys MUST come from the hardware RNG.
  // esp_random() only returns true entropy once the RF subsystem (WiFi/BT)
  // is running — which is LATER than this first-boot identity init. Enable
  // the bootloader entropy source (valid before RF) for the duration of
  // key generation, then disable it again before sensors / WiFi come up.
  const bool generating = need_node || need_secret || need_ed;
  if (generating) bootloader_random_enable();

  if (need_node) {
    random_bytes(node_id_buf, kNodeIdBytes);
    prefs.putBytes(kNvsKeyNodeId, node_id_buf, kNodeIdBytes);
    Serial.println("[identity] generated new node id");
  }
  node_id_hex_ = to_hex(node_id_buf, kNodeIdBytes);

  if (need_secret) {
    random_bytes(signing_secret_, kSigningSecretLen);
    prefs.putBytes(kNvsKeySecret, signing_secret_, kSigningSecretLen);
    Serial.println("[identity] generated new signing secret");
  }

  // ed25519 device key (ADR-0003): the 32-byte seed IS the private key
  // (RFC 8032); the public key derives deterministically, so we only
  // persist the seed.
  if (need_ed) {
    random_bytes(ed_seed_, sizeof(ed_seed_));
    prefs.putBytes(kNvsKeyEdSeed, ed_seed_, sizeof(ed_seed_));
    Serial.println("[identity] generated new ed25519 key");
  }

  if (generating) bootloader_random_disable();

  Ed25519::derivePublicKey(ed_pub_, ed_seed_);
  ed_pubkey_hex_ = to_hex_lower(ed_pub_, sizeof(ed_pub_));

  prefs.end();

  Serial.printf("[identity] node_id=%s\n", node_id_hex_.c_str());
  Serial.printf("[identity] ed25519_pub=%s\n", ed_pubkey_hex_.c_str());
}

const String& node_id_hex() {
  return node_id_hex_;
}

const uint8_t* signing_secret() {
  return signing_secret_;
}

const String& ap_password() {
  // Lazy: only touch NVS on first call. Most boots will never need
  // the AP password (WiFi credentials are persisted), so we don't
  // amortize NVS reads in begin().
  if (ap_password_.length() > 0) {
    return ap_password_;
  }

  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);

  String stored = prefs.getString(kNvsKeyAPPw, "");
  if (stored.length() >= 8) {
    ap_password_ = stored;
    prefs.end();
    // Intentionally NOT printed to serial — see header comment. The
    // operator already noted it on first boot; recovering a lost
    // password is an NVS-wipe operation.
    return ap_password_;
  }

  // First call ever — generate and persist.
  ap_password_ = generate_ap_password(ORCHARD_AP_PASSWORD_LEN);
  prefs.putString(kNvsKeyAPPw, ap_password_);
  prefs.end();

  Serial.println("[identity] generated soft-AP password (record this; "
                 "it will NOT be re-printed):");
  Serial.printf("[identity]   ap_password=%s\n", ap_password_.c_str());

  return ap_password_;
}

void hmac_sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
  const mbedtls_md_info_t* md =
      mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_hmac(md,
                  signing_secret_, kSigningSecretLen,
                  data, len,
                  out);
}

const String& ed25519_pubkey_hex() {
  return ed_pubkey_hex_;
}

void ed25519_sign(const uint8_t* data, size_t len, uint8_t out[kEd25519SigLen]) {
  // rweather/Crypto Ed25519 is RFC-8032 compliant, so signatures verify
  // against the Python (cryptography) reference and the golden vectors.
  Ed25519::sign(out, ed_seed_, ed_pub_, data, len);
}

}  // namespace orchard::identity
