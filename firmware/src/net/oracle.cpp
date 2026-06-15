// SPDX-License-Identifier: Apache-2.0
#include "oracle.h"

#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "identity.h"
#include "timekeeping.h"
#include "version.h"

namespace orchard::net {

namespace {

constexpr const char* kNvsUrl = "oracle_url";

String load_url_() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
  String u = prefs.getString(kNvsUrl, "");
  prefs.end();
  return u;
}

}  // namespace

String oracle_url() {
  return load_url_();   // NVS value as-is (empty if unset) — for the console/dashboard
}

String oracle_base_url() {
  // Resolved URL the device actually talks to: the NVS override if the
  // operator set one (local-dev / LAN oracle), else the baked default
  // (ADR-0005 §5). Used for posting and for claim-code provisioning.
  String u = load_url_();
  return u.length() ? u : String(ORCHARD_DEFAULT_ORACLE_URL);
}

bool oracle_set_url(const String& url) {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.putString(kNvsUrl, url);
  prefs.end();
  return true;
}

bool oracle_post_reading(JsonDocument& payload) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[oracle] WiFi not connected; skipping POST");
    return false;
  }
  String url = oracle_base_url();  // NVS override or the baked default
  if (url.length() == 0) {
    Serial.println("[oracle] no URL configured; skipping POST");
    return false;
  }

  // Add identity fields.
  payload["schema"]  = 1;  // ADR-0006/T14: payload format version (start at 1)
  payload["node_id"] = identity::node_id_hex();
  payload["fw"]      = orchard::kFirmwareVersion;
  payload["ts_ms"]   = (uint32_t)millis();  // monotonic per boot (not wall-clock)
  payload["seq"]     = identity::next_seq();  // replay protection — inside the
                                              // HMAC'd body, so it can't be
                                              // bumped on a captured packet
  // D6/T6: real UTC epoch seconds once SNTP has synced (GPS UTC, when a
  // fix exists, is also in sensors.gps.utc). Omitted until first sync so a
  // pre-sync reading never carries a bogus 1970 timestamp.
  const uint32_t epoch = utc_now();
  if (epoch > 0) {
    payload["ts"] = epoch;
  }

  String body;
  serializeJson(payload, body);

  // Sign the canonical body. The oracle recomputes HMAC over the
  // received body and compares.
  uint8_t sig[32];
  identity::hmac_sha256(reinterpret_cast<const uint8_t*>(body.c_str()),
                        body.length(), sig);
  String sig_hex = identity::to_hex(sig, sizeof(sig));

  HTTPClient http;
  // https gets a TLS client with cert verification disabled — the oracle is
  // Cloudflare-fronted and every body is signature-protected (HMAC +
  // secp256r1), so transport auth isn't what guarantees integrity. `secure`
  // must outlive the request (HTTPClient borrows it). Pinning the CA is a
  // hardening follow-up (T10 track).
  WiFiClientSecure secure;
  bool began;
  if (url.startsWith("https://")) {
    secure.setInsecure();
    began = http.begin(secure, url);
  } else {
    began = http.begin(url);
  }
  if (!began) {
    Serial.printf("[oracle] http.begin failed for %s\n", url.c_str());
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Orchard-Node", identity::node_id_hex());
  http.addHeader("X-Orchard-Sig",  sig_hex);
  http.setTimeout(10000);

  const int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("[oracle] POST error: %s\n",
                  HTTPClient::errorToString(code).c_str());
    http.end();
    return false;
  }
  Serial.printf("[oracle] POST -> %d (%u bytes)\n", code, (unsigned)body.length());
  if (code < 200 || code >= 300) {
    const String resp = http.getString();
    Serial.printf("[oracle] body: %s\n", resp.c_str());
  }
  http.end();
  return code >= 200 && code < 300;
}

}  // namespace orchard::net
