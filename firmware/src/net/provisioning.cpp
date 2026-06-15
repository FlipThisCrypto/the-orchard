// SPDX-License-Identifier: Apache-2.0
#include "provisioning.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "identity.h"
#include "oracle.h"
#include "version.h"
#include "wifi_mgr.h"

namespace orchard::net {

namespace {

constexpr const char* kNvsKeyProvisioned = "provisioned";
constexpr const char* kNvsUrlKey = "oracle_url";  // matches oracle.cpp

bool provisioned_ = false;
uint32_t last_attempt_ms_ = 0;
bool announced_banner_ = false;

// Begin an HTTPClient against `url`. https gets a TLS client with cert
// verification disabled: the oracle is Cloudflare-fronted and every payload
// is signature-protected (HMAC + secp256r1), so transport auth isn't what
// guarantees integrity here. Pinning the CA is a hardening follow-up.
bool http_begin(HTTPClient& http, WiFiClientSecure& secure, const String& url) {
  if (url.startsWith("https://")) {
    secure.setInsecure();
    return http.begin(secure, url);
  }
  return http.begin(url);
}

bool nvs_has_oracle_url() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
  String u = prefs.getString(kNvsUrlKey, "");
  prefs.end();
  return u.length() > 0;
}

void set_provisioned(bool v) {
  provisioned_ = v;
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.putBool(kNvsKeyProvisioned, v);
  prefs.end();
}

String grouped_code() {
  // XXXX-XXXX for readability when shown to the operator.
  const String& c = identity::claim_code();
  return (c.length() == 8) ? c.substring(0, 4) + "-" + c.substring(4) : c;
}

void print_claim_banner() {
  Serial.println("[provision] ---------------------------------------------");
  Serial.printf("[provision] UNCLAIMED. Claim code: %s\n", grouped_code().c_str());
  Serial.printf("[provision] Claim this Tree at: %s/claim\n", oracle_base_url().c_str());
  Serial.println("[provision] (wallet + Orchard Pass required)");
  Serial.println("[provision] ---------------------------------------------");
}

void announce() {
  HTTPClient http;
  WiFiClientSecure secure;
  const String url = oracle_base_url() + "/provision/announce";
  if (!http_begin(http, secure, url)) {
    Serial.println("[provision] announce: http.begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(10000);

  JsonDocument doc;
  doc["node_id"]         = identity::node_id_hex();
  doc["signing_key_hex"] = identity::to_hex(identity::signing_secret(),
                                            identity::kSigningSecretLen);
  doc["claim_code"]      = identity::claim_code();
  doc["fw_version"]      = orchard::kFirmwareVersion;
  String body;
  serializeJson(doc, body);

  const int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("[provision] announce POST error: %s\n",
                  HTTPClient::errorToString(code).c_str());
  } else if (code == 201 || code == 200) {
    // Body may say {"claimed": true} if a prior claim already bound us.
    JsonDocument resp;
    if (deserializeJson(resp, http.getString()) == DeserializationError::Ok &&
        resp["claimed"].is<bool>() && resp["claimed"].as<bool>()) {
      Serial.println("[provision] announce: oracle reports already claimed.");
      set_provisioned(true);
    }
  } else {
    Serial.printf("[provision] announce -> HTTP %d\n", code);
  }
  http.end();
}

// Returns true if the claim endpoint reports this code claimed.
bool poll_claimed() {
  HTTPClient http;
  WiFiClientSecure secure;
  const String url = oracle_base_url() + "/provision/" + identity::claim_code();
  if (!http_begin(http, secure, url)) return false;
  http.setTimeout(10000);
  const int code = http.GET();
  bool claimed = false;
  if (code == 200) {
    JsonDocument resp;
    if (deserializeJson(resp, http.getString()) == DeserializationError::Ok) {
      claimed = resp["claimed"].is<bool>() && resp["claimed"].as<bool>();
    }
  }
  http.end();
  return claimed;
}

}  // namespace

void provisioning_begin() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  const bool has_flag = prefs.isKey(kNvsKeyProvisioned);
  provisioned_ = prefs.getBool(kNvsKeyProvisioned, false);
  prefs.end();

  if (!has_flag && nvs_has_oracle_url()) {
    // Grandfather: a device configured the old way (dashboard wizard set an
    // oracle URL) is already in service — don't make it re-enter the claim
    // flow. Record the flag so this only happens once.
    set_provisioned(true);
    Serial.println("[provision] existing config detected; marked provisioned.");
    return;
  }

  if (provisioned_) {
    Serial.println("[provision] provisioned.");
  } else {
    Serial.println("[provision] unprovisioned — will announce + await claim.");
    print_claim_banner();
  }
}

bool is_provisioned() {
  return provisioned_;
}

void provisioning_loop() {
  if (provisioned_ || !wifi_connected()) return;

  const uint32_t now = millis();
  if (announced_banner_ && (now - last_attempt_ms_ < ORCHARD_PROVISION_POLL_MS)) {
    return;
  }
  last_attempt_ms_ = now;
  announced_banner_ = true;

  announce();
  if (provisioned_) return;   // announce() may have learned we're claimed

  if (poll_claimed()) {
    set_provisioned(true);
    Serial.println("[provision] CLAIMED — Tree is bound to a wallet. "
                   "Starting normal reading posts.");
  } else {
    print_claim_banner();   // reminder while we wait
  }
}

}  // namespace orchard::net
