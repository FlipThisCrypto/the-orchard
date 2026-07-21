// SPDX-License-Identifier: Apache-2.0
#include "serial_console.h"

#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>

#include "config.h"
#include "identity.h"
#include "oracle.h"
#include "ota.h"
#include "sensors/gps_neo.h"
#include "sensors/sensor.h"
#include "version.h"
#include "wifi_mgr.h"

// Phase 9.0: per-build board identifier. Comes from -D in
// platformio.ini for each env (wroom32u, freenove-s3, ...).
// Fall back to "generic" if a build forgot to set it — that way
// HW_INFO still has a valid JSON value, just an unhelpful one.
#ifndef ORCHARD_BOARD_HINT
#define ORCHARD_BOARD_HINT "generic"
#endif

namespace orchard::net {

namespace {

String line_buf_;
SampleNowFn sample_cb_ = nullptr;

void cmd_status_(const String& /*args*/) {
  String s;
  s.reserve(256);
  s += "OK {\"fw\":\"";
  s += orchard::kFirmwareVersion;
  s += "\",\"node_id\":\"";
  s += identity::node_id_hex();
  s += "\",\"wifi\":\"";
  s += wifi_status_string();
  s += "\",\"oracle\":\"";
  s += oracle_url();
  s += "\",\"uptime_ms\":";
  s += millis();
  s += "}";
  Serial.println(s);
}

// Phase 9.0: machine-readable hardware fingerprint.
//
// Emits one line:
//   OK {"fw":"0.4.0","chip":"ESP32-S3","board":"freenove-s3",
//       "node_id":"<32 hex>",
//       "sensors":[{"name":"bme280","bus":"i2c","addr":118,"active":true},
//                  {"name":"mq135","bus":"analog","active":true},...]}
//
// "active" reflects what `Sensor::begin()` returned during boot — i.e.
// "did the probe actually find this device on its declared bus." So an
// inactive entry means "the firmware was compiled with support for
// this sensor but couldn't talk to it on boot" (wiring wrong, sensor
// missing, etc.), and an active entry means "the device responded."
//
// The wizard parses this to pre-fill Step 1's sensor checklist and to
// drive the multi-board firmware variant picker downstream.
void cmd_hw_info_(const String& /*args*/) {
  const auto& sensors = orchard::sensors::SensorRegistry::instance().all();

  // Use ArduinoJson to compose so we get proper escaping for free.
  // Capacity: chip/board/node_id strings ~120 bytes + per-sensor
  // ~80 bytes; sized generously to never realloc.
  JsonDocument doc;
  doc["fw"]      = orchard::kFirmwareVersion;
  doc["chip"]    = ESP.getChipModel();
  doc["board"]   = ORCHARD_BOARD_HINT;
  doc["node_id"] = identity::node_id_hex();
  doc["pubkey"]  = identity::p256_pubkey_hex();  // ADR-0003/0007 provenance key

  JsonArray arr = doc["sensors"].to<JsonArray>();
  for (const auto& s : sensors) {
    JsonObject o = arr.add<JsonObject>();
    o["name"]   = s->name();
    o["active"] = s->is_active();
    switch (s->bus_type()) {
      case orchard::sensors::BusType::kI2C:
        o["bus"]  = "i2c";
        if (const uint8_t a = s->i2c_address(); a != 0) o["addr"] = a;
        break;
      case orchard::sensors::BusType::kUART:     o["bus"] = "uart";    break;
      case orchard::sensors::BusType::kAnalog:   o["bus"] = "analog";  break;
      case orchard::sensors::BusType::kDigital:  o["bus"] = "digital"; break;
      case orchard::sensors::BusType::kInternal: o["bus"] = "internal";break;
    }
  }

  String out;
  serializeJson(doc, out);
  Serial.print("OK ");
  Serial.println(out);
}

void cmd_wifi_set_(const String& args) {
  // Expects: <ssid> <password>
  // ssid cannot contain whitespace (use WIFI_SET_RAW later if needed).
  const int sp = args.indexOf(' ');
  if (sp <= 0) { Serial.println("ERR usage: WIFI_SET <ssid> <pass>"); return; }
  const String ssid = args.substring(0, sp);
  const String pass = args.substring(sp + 1);
  const bool ok = wifi_set_credentials(ssid, pass);
  Serial.println(ok ? "OK" : "ERR could not set");
}

void cmd_sensor_mq135_(const String& args) {
  // MQ-135 is analog and can't be auto-detected, so it's off by default; the
  // operator declares it per-Tree here. Persisted in NVS; applied on next boot
  // (sensor presence is decided in begin()).
  String a = args; a.toLowerCase();
  bool on;
  if (a == "on" || a == "1" || a == "true")       on = true;
  else if (a == "off" || a == "0" || a == "false") on = false;
  else { Serial.println("ERR usage: SENSOR_MQ135 on|off"); return; }
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.putBool("mq135", on);
  prefs.end();
  Serial.printf("OK mq135=%s (reboot to apply)\n", on ? "on" : "off");
}

void dispatch_(const String& line) {
  // Split COMMAND <args...>
  int sp = line.indexOf(' ');
  String cmd  = (sp < 0) ? line : line.substring(0, sp);
  String args = (sp < 0) ? ""   : line.substring(sp + 1);
  cmd.trim();
  args.trim();

  if (cmd == "PING") {
    Serial.println("OK pong");
  } else if (cmd == "STATUS") {
    cmd_status_(args);
  } else if (cmd == "NODE_ID") {
    Serial.print("OK ");
    Serial.println(identity::node_id_hex());
  } else if (cmd == "KEY") {
    Serial.print("OK ");
    Serial.println(
        identity::to_hex(identity::signing_secret(),
                         identity::kSigningSecretLen));
  } else if (cmd == "PUBKEY") {
    // secp256r1 public key (ADR-0003/0007), compressed SEC1 hex. The
    // wizard captures this at registration and the oracle publishes it
    // in node:<id>.pubkey.
    Serial.print("OK ");
    Serial.println(identity::p256_pubkey_hex());
  } else if (cmd == "HW_INFO") {
    cmd_hw_info_(args);
  } else if (cmd == "WIFI_SET") {
    cmd_wifi_set_(args);
  } else if (cmd == "WIFI_CLEAR") {
    wifi_clear_credentials();
    Serial.println("OK cleared");
  } else if (cmd == "ORACLE_SET") {
    if (args.length() == 0) { Serial.println("ERR usage: ORACLE_SET <url>"); return; }
    oracle_set_url(args);
    Serial.println("OK");
  } else if (cmd == "SENSOR_MQ135") {
    cmd_sensor_mq135_(args);
  } else if (cmd == "SAMPLE_NOW") {
    if (sample_cb_) {
      sample_cb_();
      Serial.println("OK sampling");
    } else {
      Serial.println("ERR no sample callback");
    }
  } else if (cmd == "I2C_SCAN") {
    // Probe addresses 1..126 on the default Wire bus. Prints every
    // address that ACKs. Operator runs this when an I2C sensor's
    // driver `begin()` returns false to figure out whether the
    // sensor is even on the bus.
    String result = "OK ";
    int count = 0;
    for (uint8_t addr = 1; addr < 127; ++addr) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) {
        char buf[8];
        snprintf(buf, sizeof(buf), "0x%02X ", addr);
        result += buf;
        ++count;
      }
    }
    if (count == 0) result += "(no devices)";
    Serial.println(result);
  } else if (cmd == "GPS_RAW") {
    // Stream raw GPS UART bytes for 3 seconds. Operator runs this
    // when `satellites: 0` to confirm wiring at the UART level
    // (NMEA sentences arriving = wires good; silence = wires wrong,
    // GPS unpowered, or antenna unplugged).
    Serial.println("OK gps_raw_start");
    orchard::sensors::gps_dump_raw(3000);
    Serial.println("OK gps_raw_end");
  } else if (cmd == "OTA_ARM") {
    // Open a 2-minute window during which POST /ota will accept a
    // firmware upload. Requires this local serial command, so a remote
    // LAN host can't flash the device (C1 hardening).
    orchard::net::ota_arm(120000);
    Serial.println("OK ota armed 120s");
  } else if (cmd == "OTA_REQUIRE_SIG") {
    // T7: flip signature enforcement. Default false (warn mode). Only
    // enable after release images are signed and this Tree runs a build
    // that knows the release public key (docs/security/SIGNED_OTA.md).
    String a = args;
    a.trim();
    if (a == "1" || a.equalsIgnoreCase("true") || a.equalsIgnoreCase("on")) {
      orchard::net::ota_set_require_signature(true);
      Serial.println("OK ota_require_signature=true");
    } else if (a == "0" || a.equalsIgnoreCase("false") || a.equalsIgnoreCase("off")) {
      orchard::net::ota_set_require_signature(false);
      Serial.println("OK ota_require_signature=false");
    } else {
      Serial.printf("OK ota_require_signature=%s (usage: OTA_REQUIRE_SIG 0|1)\n",
                    orchard::net::ota_require_signature() ? "true" : "false");
    }
  } else if (cmd == "REBOOT") {
    Serial.println("OK rebooting");
    Serial.flush();
    delay(200);
    ESP.restart();
  } else if (cmd.length() == 0) {
    // ignore empty
  } else {
    Serial.println("ERR unknown");
  }
}

}  // namespace

void console_begin() {
  Serial.begin(ORCHARD_CONSOLE_BAUD);
  delay(50);
  Serial.println();
  Serial.println("=== The Orchard — Tree firmware ===");
  Serial.printf("fw=%s node_id=%s\n",
                orchard::kFirmwareVersion,
                identity::node_id_hex().c_str());
  Serial.println("Type 'STATUS' for current state, 'PING' to test.");
}

void console_feed_byte(uint8_t b) {
  // One byte of typed/console input, routed here by the Improv serial pump
  // for every byte that is NOT part of an Improv packet. Same line assembly
  // and dispatch as the pre-0.5.0 console_loop(), so typed commands behave
  // identically (a human never types the binary "IMPROV" header, so Improv
  // framing never swallows a console byte).
  const char c = (char)b;
  if (c == '\r') return;
  if (c == '\n') {
    String l = line_buf_;
    l.trim();
    line_buf_ = "";
    if (l.length() > 0) dispatch_(l);
  } else {
    if (line_buf_.length() < 512) line_buf_ += c;
  }
}

void console_loop() {
  // No-op as of fw 0.5.0: the single Serial reader lives in
  // improv_serial_pump(), which routes non-Improv bytes to
  // console_feed_byte(). Kept so main.cpp's loop() reads naturally and as a
  // hook for any future non-input periodic console work.
}

void console_set_sample_callback(SampleNowFn fn) {
  sample_cb_ = fn;
}

}  // namespace orchard::net
