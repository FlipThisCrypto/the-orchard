// SPDX-License-Identifier: Apache-2.0
#include "oracle.h"

#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>

#include "config.h"
#include "device_reading.h"
#include "identity.h"
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
  return load_url_();
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
  String url = load_url_();
  if (url.length() == 0) {
    Serial.println("[oracle] no URL configured; skipping POST");
    return false;
  }

  // Add identity fields.
  payload["node_id"] = identity::node_id_hex();
  payload["fw"]      = orchard::kFirmwareVersion;
  // Wall-clock ts preferred; attach_device_reading overwrites when GPS/SNTP
  // provides one. millis() is only a last-resort placeholder.
  if (!payload["ts_ms"].is<int64_t>() && !payload["ts_ms"].is<int>()) {
    payload["ts_ms"] = static_cast<int64_t>(millis());
  }

  // ADR-0003: attach secp256r1-signed SPEC reading for DataLayer publish.
  // Failure is non-fatal — sensors blob + HMAC transport still work.
  orchard::attach_device_reading(payload);

  String body;
  serializeJson(payload, body);

  // Sign the canonical body. The oracle recomputes HMAC over the
  // received body and compares.
  uint8_t sig[32];
  identity::hmac_sha256(reinterpret_cast<const uint8_t*>(body.c_str()),
                        body.length(), sig);
  String sig_hex = identity::to_hex(sig, sizeof(sig));

  HTTPClient http;
  if (!http.begin(url)) {
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
