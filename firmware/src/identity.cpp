// SPDX-License-Identifier: Apache-2.0
#include "identity.h"

#include <Preferences.h>
#include <bootloader_random.h>
#include <esp_random.h>
#include <mbedtls/bignum.h>
#include <mbedtls/ecdsa.h>
#include <mbedtls/ecp.h>
#include <mbedtls/md.h>

#include "config.h"

namespace orchard::identity {

namespace {

constexpr size_t kNodeIdBytes = 16;
constexpr const char* kNvsKeyNodeId  = "node_id";
constexpr const char* kNvsKeySecret  = "sign_key";
constexpr const char* kNvsKeyP256    = "p256_key";
// Pre-ADR-0007 ed25519 seed; wiped if present (devices re-provision, the
// fleet doesn't exist yet — exactly why the curve switch happens NOW).
constexpr const char* kNvsKeyEdSeedLegacy = "ed_seed";
constexpr const char* kNvsKeyAPPw    = "ap_pw";
constexpr const char* kNvsKeySeq     = "seq_wm";   // persisted seq watermark
constexpr uint32_t kSeqReserve       = 256;        // NVS write amortization

// Printable alphabet for AP passwords. Skips characters that are
// hard to read or hard to type on a phone:  0 O o 1 l I.
constexpr const char* kAPPwAlphabet =
    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
constexpr size_t kAPPwAlphabetLen = 56;

uint8_t signing_secret_[kSigningSecretLen] = {0};
uint8_t p256_d_[kP256PrivLen] = {0};      // P-256 private scalar (big-endian)
uint8_t p256_pub_[kP256PubLen] = {0};     // compressed SEC1 public point
uint32_t seq_counter_   = 0;   // live counter (RAM)
uint32_t seq_watermark_ = 0;   // highest value guaranteed unused after a crash
String node_id_hex_;
String p256_pubkey_hex_;
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

// mbedTLS f_rng adapter over random_bytes(). Used for keygen and for
// ECDSA blinding; the signature nonce itself is RFC 6979 (no RNG).
int mbedtls_rng_cb(void* /*ctx*/, unsigned char* out, size_t len) {
  random_bytes(out, len);
  return 0;
}

// Derive + cache the compressed public key from the stored scalar.
// Returns true on success.
bool p256_load_pubkey() {
  mbedtls_ecp_group grp;
  mbedtls_mpi d;
  mbedtls_ecp_point Q;
  mbedtls_ecp_group_init(&grp);
  mbedtls_mpi_init(&d);
  mbedtls_ecp_point_init(&Q);

  int rc = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1);
  if (rc == 0) rc = mbedtls_mpi_read_binary(&d, p256_d_, sizeof(p256_d_));
  // f_rng enables coordinate randomization (side-channel blinding).
  if (rc == 0) rc = mbedtls_ecp_mul(&grp, &Q, &d, &grp.G, mbedtls_rng_cb, nullptr);
  size_t olen = 0;
  if (rc == 0) {
    rc = mbedtls_ecp_point_write_binary(&grp, &Q, MBEDTLS_ECP_PF_COMPRESSED,
                                        &olen, p256_pub_, sizeof(p256_pub_));
  }

  mbedtls_ecp_point_free(&Q);
  mbedtls_mpi_free(&d);
  mbedtls_ecp_group_free(&grp);

  if (rc != 0 || olen != kP256PubLen) {
    Serial.printf("[identity] p256 pubkey derivation failed: -0x%04x\n",
                  static_cast<unsigned>(-rc));
    return false;
  }
  return true;
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
  const bool need_p256 =
      prefs.getBytes(kNvsKeyP256, p256_d_, sizeof(p256_d_))
          != sizeof(p256_d_);

  // H5 hardening: persistent device keys MUST come from the hardware RNG.
  // esp_random() only returns true entropy once the RF subsystem (WiFi/BT)
  // is running — which is LATER than this first-boot identity init. Enable
  // the bootloader entropy source (valid before RF) for the duration of
  // key generation, then disable it again before sensors / WiFi come up.
  const bool generating = need_node || need_secret || need_p256;
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

  // secp256r1 device key (ADR-0007): generate a keypair so the scalar is
  // guaranteed in [1, n-1] (a raw random 32-byte value isn't, always);
  // only the scalar is persisted — the public key re-derives on boot.
  if (need_p256) {
    mbedtls_ecp_group grp;
    mbedtls_mpi d;
    mbedtls_ecp_point Q;
    mbedtls_ecp_group_init(&grp);
    mbedtls_mpi_init(&d);
    mbedtls_ecp_point_init(&Q);
    int rc = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1);
    if (rc == 0) rc = mbedtls_ecp_gen_keypair(&grp, &d, &Q, mbedtls_rng_cb, nullptr);
    if (rc == 0) rc = mbedtls_mpi_write_binary(&d, p256_d_, sizeof(p256_d_));
    mbedtls_ecp_point_free(&Q);
    mbedtls_mpi_free(&d);
    mbedtls_ecp_group_free(&grp);
    if (rc == 0) {
      prefs.putBytes(kNvsKeyP256, p256_d_, sizeof(p256_d_));
      Serial.println("[identity] generated new secp256r1 key");
    } else {
      Serial.printf("[identity] p256 keygen FAILED: -0x%04x\n",
                    static_cast<unsigned>(-rc));
    }
  }

  if (generating) bootloader_random_disable();

  // One-time cleanup of the pre-ADR-0007 ed25519 seed, if this board had one.
  if (prefs.isKey(kNvsKeyEdSeedLegacy)) {
    prefs.remove(kNvsKeyEdSeedLegacy);
    Serial.println("[identity] removed legacy ed25519 seed (ADR-0007)");
  }

  if (p256_load_pubkey()) {
    p256_pubkey_hex_ = to_hex_lower(p256_pub_, sizeof(p256_pub_));
  }

  prefs.end();

  Serial.printf("[identity] node_id=%s\n", node_id_hex_.c_str());
  Serial.printf("[identity] p256_pub=%s\n", p256_pubkey_hex_.c_str());
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

uint32_t next_seq() {
  // Lazy init on first call: resume from the persisted watermark.
  // Everything below the watermark may already have been used before
  // the last reboot/crash, so we start AT the watermark.
  if (seq_watermark_ == 0) {
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
    seq_watermark_ = prefs.getUInt(kNvsKeySeq, 0);
    if (seq_watermark_ == 0) {
      // First boot ever: claim the first block.
      seq_watermark_ = kSeqReserve;
      prefs.putUInt(kNvsKeySeq, seq_watermark_);
    }
    prefs.end();
    seq_counter_ = seq_watermark_ - kSeqReserve;
  }

  ++seq_counter_;

  // Crossing the reservation boundary: persist the next block before
  // handing out a number from it.
  if (seq_counter_ >= seq_watermark_) {
    seq_watermark_ = seq_counter_ + kSeqReserve;
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
    prefs.putUInt(kNvsKeySeq, seq_watermark_);
    prefs.end();
  }

  return seq_counter_;
}

const String& p256_pubkey_hex() {
  return p256_pubkey_hex_;
}

void p256_sign(const uint8_t* data, size_t len, uint8_t out[kP256SigLen]) {
  memset(out, 0, kP256SigLen);

  // sha256 digest of the exact payload bytes (SPEC §4). mbedtls_md() is
  // stable across the mbedTLS 2.x/3.x API split, unlike mbedtls_sha256*.
  uint8_t hash[32];
  const mbedtls_md_info_t* md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mbedtls_md(md, data, len, hash) != 0) return;

  mbedtls_ecp_group grp;
  mbedtls_mpi d, r, s, half_n;
  mbedtls_ecp_group_init(&grp);
  mbedtls_mpi_init(&d);
  mbedtls_mpi_init(&r);
  mbedtls_mpi_init(&s);
  mbedtls_mpi_init(&half_n);

  int rc = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1);
  if (rc == 0) rc = mbedtls_mpi_read_binary(&d, p256_d_, sizeof(p256_d_));
  // RFC 6979 deterministic nonce — byte-identical to the Python reference
  // and the golden vectors. f_rng is only the side-channel blinding source.
  if (rc == 0) {
    rc = mbedtls_ecdsa_sign_det_ext(&grp, &r, &s, &d, hash, sizeof(hash),
                                    MBEDTLS_MD_SHA256, mbedtls_rng_cb, nullptr);
  }
  if (rc == 0) {
    // Low-S normalization (SPEC §4): s = min(s, n - s). Keeps every
    // verifier happy, including strict ones, and stays deterministic.
    rc = mbedtls_mpi_copy(&half_n, &grp.N);
    if (rc == 0) rc = mbedtls_mpi_shift_r(&half_n, 1);
    if (rc == 0 && mbedtls_mpi_cmp_mpi(&s, &half_n) > 0) {
      rc = mbedtls_mpi_sub_mpi(&s, &grp.N, &s);
    }
  }
  if (rc == 0) rc = mbedtls_mpi_write_binary(&r, out, 32);
  if (rc == 0) rc = mbedtls_mpi_write_binary(&s, out + 32, 32);
  if (rc != 0) {
    memset(out, 0, kP256SigLen);
    Serial.printf("[identity] p256_sign failed: -0x%04x\n",
                  static_cast<unsigned>(-rc));
  }

  mbedtls_mpi_free(&half_n);
  mbedtls_mpi_free(&s);
  mbedtls_mpi_free(&r);
  mbedtls_mpi_free(&d);
  mbedtls_ecp_group_free(&grp);
}

}  // namespace orchard::identity
