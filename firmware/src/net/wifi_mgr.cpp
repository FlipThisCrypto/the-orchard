// SPDX-License-Identifier: Apache-2.0
#include "wifi_mgr.h"

#include <Preferences.h>
#include <WiFi.h>

#include "config.h"

namespace orchard::net {

namespace {

constexpr const char* kNvsSsid = "wifi_ssid";
constexpr const char* kNvsPass = "wifi_pass";

String cached_ssid_;
String cached_pass_;
uint32_t last_reconnect_attempt_ = 0;
constexpr uint32_t kReconnectIntervalMs = 30000;

// Async-connect state machine. try_connect_() kicks WiFi.begin() then
// returns immediately. wifi_loop() polls WiFi.status() and logs the
// outcome (connected | timeout) when the in-flight attempt resolves.
// Without this, EVERY blocking-wait inside the main loop (sample tick,
// console dispatch, ota_loop) was stalled for up to
// ORCHARD_WIFI_CONNECT_TIMEOUT_MS — which is exactly what made the
// wizard's WIFI_SET-followed-by-ORACLE_SET sequence time out: the
// ORACLE_SET command arrived while wifi_loop was mid-blocking-wait
// from the connect kicked by WIFI_SET, so console_loop couldn't run.
bool connect_in_flight_ = false;
uint32_t connect_started_at_ = 0;
// Last time wifi_loop polled WiFi.status() while connect_in_flight_
// was true. We throttle to ~once every 250ms instead of every 10ms
// (the main loop's delay) — hammering WiFi.status() 100x/sec while
// the radio is doing its connect handshake starves the WiFi internal
// task on some boards and can manifest as connect failures or
// brownouts. 250ms matches the cadence the previous blocking
// try_connect_() spin used to use (delay(250) between status checks).
uint32_t last_status_check_ = 0;
constexpr uint32_t kStatusCheckIntervalMs = 250;

void load_creds_() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
  cached_ssid_ = prefs.getString(kNvsSsid, "");
  cached_pass_ = prefs.getString(kNvsPass, "");
  prefs.end();
}

void try_connect_() {
  if (cached_ssid_.length() == 0) {
    Serial.println("[wifi] no creds stored; idle. Use WIFI_SET over serial.");
    return;
  }
  if (connect_in_flight_) {
    // Already in flight; let wifi_loop resolve it. Avoids stacking
    // multiple WiFi.begin() calls if WIFI_SET races a wifi_loop tick.
    return;
  }
  Serial.printf("[wifi] connecting to '%s'\n", cached_ssid_.c_str());
  WiFi.mode(WIFI_STA);
  WiFi.begin(cached_ssid_.c_str(), cached_pass_.c_str());
  connect_in_flight_ = true;
  connect_started_at_ = millis();
}

}  // namespace

void wifi_begin() {
  load_creds_();
  try_connect_();
}

void wifi_loop() {
  // If a connect attempt is in flight, poll its status and log the
  // result on transition. Non-blocking — returns within microseconds.
  // Throttle status reads to kStatusCheckIntervalMs (250ms) to avoid
  // starving the WiFi internal task.
  if (connect_in_flight_) {
    const uint32_t now = millis();
    if (now - last_status_check_ < kStatusCheckIntervalMs) return;
    last_status_check_ = now;
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[wifi] connected, ip=%s rssi=%d\n",
                    WiFi.localIP().toString().c_str(),
                    WiFi.RSSI());
      connect_in_flight_ = false;
    } else if (now - connect_started_at_ >=
               ORCHARD_WIFI_CONNECT_TIMEOUT_MS) {
      Serial.println("[wifi] connect timeout; will retry");
      connect_in_flight_ = false;
      last_reconnect_attempt_ = now;
    }
    return;
  }

  if (WiFi.status() == WL_CONNECTED) return;
  const uint32_t now = millis();
  if (now - last_reconnect_attempt_ < kReconnectIntervalMs) return;
  last_reconnect_attempt_ = now;
  try_connect_();
}

bool wifi_connected() {
  return WiFi.status() == WL_CONNECTED;
}

String wifi_status_string() {
  if (!wifi_connected()) {
    return cached_ssid_.length() ? "disconnected" : "unconfigured";
  }
  String s;
  s.reserve(64);
  s += "connected ssid=";
  s += cached_ssid_;
  s += " ip=";
  s += WiFi.localIP().toString();
  s += " rssi=";
  s += WiFi.RSSI();
  return s;
}

bool wifi_set_credentials(const String& ssid, const String& password) {
  if (ssid.length() == 0) return false;
  {
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
    prefs.putString(kNvsSsid, ssid);
    prefs.putString(kNvsPass, password);
    prefs.end();
  }
  cached_ssid_ = ssid;
  cached_pass_ = password;

  // Non-blocking: drop the current connection so the next wifi_loop()
  // tick picks up the new creds. We do NOT call try_connect_() here —
  // that's a synchronous spin that blocks for ORCHARD_WIFI_CONNECT_TIMEOUT_MS
  // (~10s) and starves the serial console. The console caller (e.g.
  // the dashboard wizard's WIFI_SET) needs an immediate OK ack; if we
  // block, the dashboard's serial timeout fires and the wizard reports
  // "Push WiFi credentials" as failed even though the creds DID land.
  //
  // The throttle reset below makes wifi_loop() try the new creds on
  // its very next pass instead of waiting up to kReconnectIntervalMs.
  // The result is identical to the old synchronous version from the
  // operator's POV (Tree connects within seconds) without the
  // console-blocking side effect.
  WiFi.disconnect(true);
  last_reconnect_attempt_ = millis() - kReconnectIntervalMs;
  return true;
}

void wifi_clear_credentials() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.remove(kNvsSsid);
  prefs.remove(kNvsPass);
  prefs.end();
  cached_ssid_ = "";
  cached_pass_ = "";
}

}  // namespace orchard::net
