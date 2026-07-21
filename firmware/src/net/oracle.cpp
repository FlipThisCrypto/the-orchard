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

// NVS stores the POST path (…/readings). Beacon is at the oracle origin.
String beacon_url_from_readings_(const String& readings_url) {
  // Strip trailing /readings or /readings/ → /beacon
  String base = readings_url;
  while (base.endsWith("/")) {
    base.remove(base.length() - 1);
  }
  const int idx = base.lastIndexOf('/');
  if (idx > (int)strlen("http://")) {
    const String last = base.substring(idx + 1);
    if (last.equalsIgnoreCase("readings")) {
      base = base.substring(0, idx);
    }
  }
  return base + "/beacon";
}

// Throttle beacon refresh: reuse anchor for ~5 minutes.
uint32_t last_beacon_ms_ = 0;
constexpr uint32_t kBeaconRefreshMs = 5 * 60 * 1000;

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

bool oracle_refresh_beacon() {
  if (WiFi.status() != WL_CONNECTED) return false;
  String readings = load_url_();
  if (readings.length() == 0) return false;

  // Skip if we refreshed recently (unless never).
  if (last_beacon_ms_ != 0 &&
      (millis() - last_beacon_ms_) < kBeaconRefreshMs) {
    return true;
  }

  const String url = beacon_url_from_readings_(readings);
  HTTPClient http;
  if (!http.begin(url)) {
    Serial.printf("[oracle] beacon begin failed: %s\n", url.c_str());
    return false;
  }
  http.setTimeout(5000);
  const int code = http.GET();
  if (code != 200) {
    Serial.printf("[oracle] beacon GET -> %d\n", code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  JsonDocument doc;
  if (deserializeJson(doc, body)) {
    Serial.println("[oracle] beacon JSON parse failed");
    return false;
  }
  const char* anchor = doc["block_anchor"] | "";
  if (strlen(anchor) < 16) {
    Serial.println("[oracle] beacon missing block_anchor");
    return false;
  }
  orchard::set_block_anchor(anchor);
  last_beacon_ms_ = millis();
  Serial.printf("[oracle] beacon anchor=%s height=%ld source=%s\n",
                anchor,
                static_cast<long>(doc["block_height"] | 0),
                doc["source"] | "?");
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

  // SPEC §4.2: refresh block_anchor before signing the device reading.
  oracle_refresh_beacon();

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
