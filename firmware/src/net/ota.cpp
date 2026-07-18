// SPDX-License-Identifier: Apache-2.0
#include "ota.h"

#include <Arduino.h>
#include <Preferences.h>
#include <Update.h>
#include <WebServer.h>
#include <WiFi.h>
#include <mbedtls/ecdsa.h>
#include <mbedtls/ecp.h>
#include <mbedtls/md.h>

#include "config.h"
#include "identity.h"
#include "ota_release_pubkey.h"
#include "version.h"

namespace orchard::net {

namespace {

WebServer server_(ORCHARD_DEVICE_HTTP_PORT);
bool started_ = false;
// OTA arm window (C1 hardening). /ota only accepts an upload while armed;
// arming requires the local OTA_ARM serial command.
uint32_t armed_until_ms_ = 0;
bool upload_rejected_ = false;
bool sig_invalid_ = false;

// Running SHA-256 of the streamed firmware image (T7 signed OTA).
mbedtls_md_context_t ota_md_ctx_;
bool ota_md_active_ = false;
uint8_t ota_digest_[32] = {0};
// Detached signature from X-Orchard-Ota-Sig (128 hex chars = 64 bytes).
uint8_t ota_sig_[identity::kP256SigLen] = {0};
bool ota_sig_present_ = false;

// NVS: enforce signature (default false = warn mode).
constexpr const char* kNvsKeyOtaReqSig = "ota_req_sig";
bool ota_require_sig_cached_ = false;
bool ota_require_sig_loaded_ = false;

bool ota_is_armed_() {
  return armed_until_ms_ != 0 && (int32_t)(armed_until_ms_ - millis()) > 0;
}

bool hex_nibble_(char c, uint8_t* out) {
  if (c >= '0' && c <= '9') { *out = (uint8_t)(c - '0'); return true; }
  if (c >= 'a' && c <= 'f') { *out = (uint8_t)(c - 'a' + 10); return true; }
  if (c >= 'A' && c <= 'F') { *out = (uint8_t)(c - 'A' + 10); return true; }
  return false;
}

bool parse_hex64_(const String& hex, uint8_t out[64]) {
  if (hex.length() != 128) return false;
  for (int i = 0; i < 64; i++) {
    uint8_t hi = 0, lo = 0;
    if (!hex_nibble_(hex[i * 2], &hi) || !hex_nibble_(hex[i * 2 + 1], &lo)) {
      return false;
    }
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

bool parse_hex33_(const char* hex, uint8_t out[33]) {
  if (hex == nullptr) return false;
  size_t n = strlen(hex);
  if (n != 66) return false;
  for (int i = 0; i < 33; i++) {
    uint8_t hi = 0, lo = 0;
    if (!hex_nibble_(hex[i * 2], &hi) || !hex_nibble_(hex[i * 2 + 1], &lo)) {
      return false;
    }
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

bool ota_require_signature_() {
  if (!ota_require_sig_loaded_) {
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
    ota_require_sig_cached_ = prefs.getBool(kNvsKeyOtaReqSig, false);
    prefs.end();
    ota_require_sig_loaded_ = true;
  }
  return ota_require_sig_cached_;
}

void ota_md_begin_() {
  if (ota_md_active_) {
    mbedtls_md_free(&ota_md_ctx_);
    ota_md_active_ = false;
  }
  mbedtls_md_init(&ota_md_ctx_);
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mbedtls_md_setup(&ota_md_ctx_, info, 0) != 0) return;
  if (mbedtls_md_starts(&ota_md_ctx_) != 0) {
    mbedtls_md_free(&ota_md_ctx_);
    return;
  }
  ota_md_active_ = true;
  memset(ota_digest_, 0, sizeof(ota_digest_));
}

void ota_md_update_(const uint8_t* data, size_t len) {
  if (!ota_md_active_ || data == nullptr || len == 0) return;
  mbedtls_md_update(&ota_md_ctx_, data, len);
}

bool ota_md_finish_() {
  if (!ota_md_active_) return false;
  int rc = mbedtls_md_finish(&ota_md_ctx_, ota_digest_);
  mbedtls_md_free(&ota_md_ctx_);
  ota_md_active_ = false;
  return rc == 0;
}

// Returns: 1 = valid, 0 = invalid, -1 = no pubkey configured (skip).
int ota_check_signature_() {
  const char* pub_hex = ORCHARD_OTA_RELEASE_PUBKEY_HEX;
  if (pub_hex == nullptr || pub_hex[0] == '\0') {
    Serial.println("[ota] no release pubkey baked in — signature check skipped");
    return -1;
  }
  if (!ota_sig_present_) {
    Serial.println("[ota] signature MISSING (no X-Orchard-Ota-Sig header)");
    return 0;
  }
  uint8_t pub[identity::kP256PubLen];
  if (!parse_hex33_(pub_hex, pub)) {
    Serial.println("[ota] release pubkey hex is malformed — treating as invalid");
    return 0;
  }
  // Verify over the precomputed digest by re-hashing through p256_verify
  // which hashes the data itself. We already hashed the stream into
  // ota_digest_; re-verify by reconstructing is not available, so we
  // verify by signing API that takes raw image — but we don't keep the
  // full image. Use mbedtls_ecdsa_verify on the digest directly via a
  // thin path: identity::p256_verify hashes again. Workaround: call
  // mbedtls here on the digest we already have.
  //
  // For correctness with identity::p256_verify we would need the full
  // image. Instead verify ECDSA over the already-computed sha256 digest.
  mbedtls_ecp_group grp;
  mbedtls_ecp_point Q;
  mbedtls_mpi r, s;
  mbedtls_ecp_group_init(&grp);
  mbedtls_ecp_point_init(&Q);
  mbedtls_mpi_init(&r);
  mbedtls_mpi_init(&s);
  int rc = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_SECP256R1);
  if (rc == 0) rc = mbedtls_ecp_point_read_binary(&grp, &Q, pub, sizeof(pub));
  if (rc == 0) rc = mbedtls_ecp_check_pubkey(&grp, &Q);
  if (rc == 0) rc = mbedtls_mpi_read_binary(&r, ota_sig_, 32);
  if (rc == 0) rc = mbedtls_mpi_read_binary(&s, ota_sig_ + 32, 32);
  if (rc == 0) {
    rc = mbedtls_ecdsa_verify(&grp, ota_digest_, sizeof(ota_digest_), &Q, &r, &s);
  }
  mbedtls_mpi_free(&s);
  mbedtls_mpi_free(&r);
  mbedtls_ecp_point_free(&Q);
  mbedtls_ecp_group_free(&grp);
  if (rc == 0) {
    Serial.println("[ota] signature OK");
    return 1;
  }
  Serial.printf("[ota] signature INVALID (mbedtls -0x%04x)\n",
                static_cast<unsigned>(-rc));
  return 0;
}

void handle_health_() {
  String body;
  body.reserve(256);
  body += "{\"node_id\":\"";
  body += identity::node_id_hex();
  body += "\",\"fw\":\"";
  body += orchard::kFirmwareVersion;
  body += "\",\"uptime_ms\":";
  body += millis();
  body += ",\"ota_require_signature\":";
  body += ota_require_signature_() ? "true" : "false";
  body += ",\"ota_pubkey_configured\":";
  body += (ORCHARD_OTA_RELEASE_PUBKEY_HEX[0] != '\0') ? "true" : "false";
  body += "}";
  server_.send(200, "application/json", body);
}

void handle_ota_upload_() {
  HTTPUpload& upload = server_.upload();
  if (upload.status == UPLOAD_FILE_START) {
    if (!ota_is_armed_()) {
      upload_rejected_ = true;
      sig_invalid_ = false;
      Serial.println("[ota] REJECTED upload — not armed (run OTA_ARM on the "
                     "serial console first)");
      return;
    }
    upload_rejected_ = false;
    sig_invalid_ = false;
    ota_sig_present_ = false;
    memset(ota_sig_, 0, sizeof(ota_sig_));

    // Detached signature header (128 hex chars). Optional until enforcement.
    if (server_.hasHeader("X-Orchard-Ota-Sig")) {
      String hx = server_.header("X-Orchard-Ota-Sig");
      ota_sig_present_ = parse_hex64_(hx, ota_sig_);
      if (!ota_sig_present_) {
        Serial.println("[ota] X-Orchard-Ota-Sig present but not 128 hex chars");
      }
    }

    Serial.printf("[ota] starting update, name=%s\n", upload.filename.c_str());
    ota_md_begin_();
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
      Update.printError(Serial);
    }
  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if (upload_rejected_) return;
    ota_md_update_(upload.buf, upload.currentSize);
    if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
      Update.printError(Serial);
    }
  } else if (upload.status == UPLOAD_FILE_END) {
    if (upload_rejected_) return;
    if (!ota_md_finish_()) {
      Serial.println("[ota] digest finalize failed");
    }
    const int sig_rc = ota_check_signature_();
    // sig_rc: 1 ok, 0 invalid/missing, -1 no pubkey configured.
    if (sig_rc == 0) {
      if (ota_require_signature_()) {
        sig_invalid_ = true;
        Update.abort();
        Serial.println("[ota] signature required — aborting Update (enforced)");
        return;
      }
      Serial.println("[ota] signature INVALID (warn mode) — flashing anyway");
    }
    if (Update.end(/*evenIfRemaining=*/true)) {
      Serial.printf("[ota] update OK, %u bytes\n", upload.totalSize);
    } else {
      Update.printError(Serial);
    }
  }
}

void handle_ota_done_() {
  if (upload_rejected_) {
    upload_rejected_ = false;
    server_.send(403, "text/plain",
                 "OTA not armed — run OTA_ARM on the serial console first");
    return;
  }
  if (sig_invalid_) {
    sig_invalid_ = false;
    server_.send(403, "text/plain",
                 "OTA signature invalid or missing (ota_require_signature)");
    return;
  }
  if (Update.hasError()) {
    server_.send(500, "text/plain", "OTA failed");
  } else {
    armed_until_ms_ = 0;  // consume the arm window on a successful flash
    server_.send(200, "text/plain", "OK; rebooting");
    delay(500);
    ESP.restart();
  }
}

}  // namespace

void ota_arm(uint32_t window_ms) {
  armed_until_ms_ = millis() + window_ms;
  Serial.printf("[ota] armed for %lu ms — POST /ota now\n",
                (unsigned long)window_ms);
}

void ota_set_require_signature(bool require) {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.putBool(kNvsKeyOtaReqSig, require);
  prefs.end();
  ota_require_sig_cached_ = require;
  ota_require_sig_loaded_ = true;
  Serial.printf("[ota] ota_require_signature=%s\n", require ? "true" : "false");
}

bool ota_require_signature() {
  return ota_require_signature_();
}

void ota_begin() {
  if (started_) return;
  // Collect custom header for detached OTA signatures.
  const char* hdrs[] = {"X-Orchard-Ota-Sig"};
  server_.collectHeaders(hdrs, 1);
  server_.on("/health", HTTP_GET, handle_health_);
  server_.on("/ota", HTTP_POST, handle_ota_done_, handle_ota_upload_);
  server_.begin();
  started_ = true;
  Serial.printf("[ota] http server listening on :%d\n",
                ORCHARD_DEVICE_HTTP_PORT);
}

void ota_loop() {
  if (!started_) {
    if (WiFi.status() == WL_CONNECTED) ota_begin();
    return;
  }
  server_.handleClient();
}

}  // namespace orchard::net
