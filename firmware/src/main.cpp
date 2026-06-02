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
//   loop()
//     console_loop()              -> handle dashboard commands
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
#include "net/oracle.h"
#include "net/ota.h"
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

  // 6. Status LED on.
  pinMode(ORCHARD_PIN_STATUS_LED, OUTPUT);
  digitalWrite(ORCHARD_PIN_STATUS_LED, HIGH);

  last_sample_ms_ = millis() - ORCHARD_SAMPLE_INTERVAL_MS;  // sample once at boot
}

void loop() {
  orchard::net::console_loop();
  orchard::net::wifi_loop();
  orchard::net::ota_loop();

  // Don't sample-and-post until WiFi is up. The sample path includes
  // DS18B20::requestTemperatures() which blocks the main task for
  // ~750ms (one full 12-bit conversion). If that blocking window
  // overlaps the WiFi connect handshake at boot, the WiFi task can
  // starve and the chip can brown out trying to TX while the main
  // task is stuck in a 1-Wire wait. The old blocking try_connect_()
  // implicitly serialized this — WiFi always finished connecting
  // before the first sample fired. The non-blocking version (0.4.5)
  // didn't, which manifested as ~12s power-cycle loops on the S3
  // dev board with CH343 bridge + DS18B20 wired up.
  //
  // Once WiFi is connected, the radio's TX bursts are short and the
  // 750ms DS18B20 read doesn't conflict.
  const uint32_t now = millis();
  if (now - last_sample_ms_ >= ORCHARD_SAMPLE_INTERVAL_MS &&
      orchard::net::wifi_connected()) {
    last_sample_ms_ = now;
    do_sample_and_post();
  }

  delay(10);
}
