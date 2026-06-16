// SPDX-License-Identifier: Apache-2.0
//
// The Orchard — Tree firmware entry point.
//
// Lifecycle:
//   setup()
//     identity::begin()           -> load/generate node_id + signing key
//     console_begin()             -> open USB-serial provisioning UI
//     SensorRegistry::begin_all() -> bring up every self-registered driver
//     wifi_begin()                -> try to connect with stored creds
//     improv_begin()              -> Improv Wi-Fi Serial (esp-web-tools)
//   loop()
//     improv_serial_pump()        -> single Serial reader: Improv + console
//     wifi_loop()                 -> reconnect on drop
//     ota_loop()                  -> serve /health + /ota when WiFi up
//     sample_loop()               -> every N seconds, sample sensors + POST

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Wire.h>

#include "config.h"
#include "identity.h"
#include "pins.h"
#include "sensors/sensor.h"
#include "net/improv_serial.h"
#include "net/oracle.h"
#include "net/ota.h"
#include "net/provisioning.h"
#include "net/serial_console.h"
#include "net/wifi_mgr.h"

namespace {

uint32_t last_sample_ms_ = 0;

void do_sample_and_post() {
  JsonDocument doc;
  JsonObject sensors_obj = doc["sensors"].to<JsonObject>();
  orchard::sensors::SensorRegistry::instance().sample_all(sensors_obj);
  orchard::net::oracle_post_reading(doc);
}

}  // namespace

void setup() {
  // 1. Identity first — sensor drivers and net layer all reference node_id.
  orchard::identity::begin();

  // 2. Console (USB serial). Always available, even with no WiFi.
  orchard::net::console_begin();
  orchard::net::console_set_sample_callback(&do_sample_and_post);

  // 3. I2C bus up so I2C sensor drivers can probe.
  Wire.begin(ORCHARD_PIN_I2C_SDA, ORCHARD_PIN_I2C_SCL);

  // 4. Sensor drivers — each is self-registered; bring them up.
  orchard::sensors::SensorRegistry::instance().begin_all();
  Serial.printf("[sensors] %u active sensor(s)\n",
                (unsigned)orchard::sensors::SensorRegistry::instance().active_count());

  // 5. WiFi (using NVS-stored creds, if any).
  orchard::net::wifi_begin();

  // 5b. Provisioning state (ADR-0005). Grandfathers a Tree that already has
  // an oracle URL in NVS; a fresh device stays unprovisioned until an
  // operator claims its code in the browser. Must run after wifi_begin()
  // (it talks to the oracle) and after identity::begin() (it needs the
  // node_id / signing key / claim code).
  orchard::net::provisioning_begin();

  // 5c. Improv Wi-Fi Serial (esp-web-tools). After a browser flashes the
  // board, esp-web-tools' "Connect to Wi-Fi" dialog provisions WiFi over this
  // same USB serial line and we hand back the claim URL so it shows a "Next"
  // button. MUST come after console_begin() (it shares the one Serial reader
  // with the console) and after wifi_begin() (so it reports the right initial
  // state). Improv ONLY sets WiFi creds — never an oracle URL — so it can't
  // trip the provisioning grandfather skip in provisioning.cpp.
  orchard::net::improv_begin();

  // 6. Status LED on.
  pinMode(ORCHARD_PIN_STATUS_LED, OUTPUT);
  digitalWrite(ORCHARD_PIN_STATUS_LED, HIGH);

  last_sample_ms_ = millis() - ORCHARD_SAMPLE_INTERVAL_MS;  // sample once at boot
}

void loop() {
  // Single Serial reader (fw 0.5.0): the Improv pump reads every byte, routes
  // Improv-protocol bytes to the Improv handler and everything else to the
  // ASCII console (console_feed_byte). console_loop() is now a no-op kept for
  // any future non-input console housekeeping.
  orchard::net::improv_serial_pump();
  orchard::net::console_loop();
  orchard::net::wifi_loop();
  orchard::net::ota_loop();

  // Drive the claim-code flow while unprovisioned (no-op once claimed /
  // grandfathered). Announces + polls on its own throttle.
  orchard::net::provisioning_loop();

  // Only sample-and-post once WiFi is up. As of 0.4.7 this is an
  // efficiency gate, not a stability one: a reading we can't POST is
  // wasted work and oracle_post_reading() already no-ops when offline.
  //
  // It used to be load-bearing. The sample path calls DS18B20, which
  // historically blocked the main task ~750ms (one 12-bit conversion).
  // That block overlapping the WiFi association handshake at boot
  // browned out the S3 (~12s power-cycle loop, fw 0.4.5). 0.4.6 added
  // this gate as a workaround; 0.4.7 made the DS18B20 driver request
  // conversions non-blocking (see sensors/ds18b20.cpp), so the block —
  // and therefore the brownout — is gone even if this gate were removed.
  // We keep it only because sampling with nowhere to send is pointless.
  // Gate automatic posting on provisioning: an unclaimed Tree has no wallet
  // binding at the oracle, so its readings would be rejected — and we don't
  // want a fresh device spamming readings before someone owns it. The manual
  // "sample now" console command stays ungated for bench testing.
  const uint32_t now = millis();
  if (now - last_sample_ms_ >= ORCHARD_SAMPLE_INTERVAL_MS &&
      orchard::net::wifi_connected() &&
      orchard::net::is_provisioned()) {
    last_sample_ms_ = now;
    do_sample_and_post();
  }

  delay(10);
}
