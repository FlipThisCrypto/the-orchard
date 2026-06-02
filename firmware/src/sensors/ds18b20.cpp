// SPDX-License-Identifier: Apache-2.0
#include "ds18b20.h"

#include <Arduino.h>
#include <stdio.h>

#include "config.h"
#include "pins.h"

namespace orchard::sensors {

bool DS18B20Sensor::begin() {
  // Bind the OneWire library to our data pin and hand it to the
  // DallasTemperature wrapper, which speaks the higher-level
  // request/wait/read protocol on top of OneWire's bit-level driver.
  wire_.begin(ORCHARD_PIN_DS18B20_DATA);
  dt_.setOneWire(&wire_);
  dt_.begin();

  device_count_ = dt_.getDeviceCount();
  if (device_count_ <= 0) {
    // No 1-Wire device responded. Most common causes (in rough order):
    //   1. Missing 4.7 kΩ pull-up resistor between DATA and VCC.
    //   2. DATA wire on the wrong GPIO (expected:
    //      ORCHARD_PIN_DS18B20_DATA — see pins.h).
    //   3. Sensor not powered (3.3V supply not connected).
    Serial.println(
        "[ds18b20] no devices on 1-Wire bus — check 4.7k pull-up "
        "between DATA and VCC, and DATA wire on the configured GPIO.");
    return false;
  }

  // Bind to the first device on the bus. getAddress returns false if
  // the index is out of range — which we just established it isn't.
  if (!dt_.getAddress(rom_, 0)) {
    return false;
  }

  // 12-bit (1/16 °C) resolution gives ~0.0625 °C steps with a ~750 ms
  // conversion time. That's fine for our 60-second sample cadence;
  // it'd be too slow if we ever shrink the sample interval.
  dt_.setResolution(rom_, 12);

  // Non-blocking conversions. By default DallasTemperature blocks inside
  // requestTemperatures() for the full ~750 ms 12-bit conversion. That
  // 750 ms block overlapping the WiFi association handshake at boot was
  // the root cause of the ESP32-S3 power-cycle loop (see LOG 2026-06-02):
  // the radio's association current draw plus a stalled main task browned
  // the chip out ~12 s in, before it could ever connect. Gating the
  // sample tick on wifi_connected() in 0.4.6 was a workaround that left
  // the block in place; setWaitForConversion(false) removes it entirely
  // so read() always returns in microseconds, connected or not.
  //
  // The pattern: request a conversion now, read its result on the NEXT
  // sample tick. With a 60 s sample interval the 750 ms conversion is
  // always finished long before we read it. Kick the first conversion
  // here so the first sample tick already has a valid result waiting.
  dt_.setWaitForConversion(false);
  dt_.requestTemperatures();

  return true;
}

bool DS18B20Sensor::read(JsonObject out) {
  // Read the conversion requested on the previous tick (or in begin()),
  // then immediately kick the next one. Because begin() called
  // setWaitForConversion(false), requestTemperatures() returns without
  // blocking — this whole call is a few microseconds, not ~750 ms.
  const float t = dt_.getTempC(rom_);
  dt_.requestTemperatures();

  // DallasTemperature reports DEVICE_DISCONNECTED_C (-127.0) when the
  // chip doesn't respond. Drop the sample if so — better to omit the
  // reading than to publish a sentinel value the oracle treats as a
  // real -127 °C measurement.
  if (t == DEVICE_DISCONNECTED_C) {
    Serial.println("[ds18b20] chip went away mid-read; dropping sample");
    return false;
  }

  out["temperature_c"] = t;
  out["device_count"]  = device_count_;

  // Surface the 64-bit ROM id (family + serial + CRC) as a hex string
  // so the oracle can tell apart multiple DS18B20s if the operator
  // adds more later. Format: 16 uppercase hex chars, no separator.
  char rom_hex[17];
  for (int i = 0; i < 8; ++i) {
    snprintf(rom_hex + i * 2, 3, "%02X", rom_[i]);
  }
  out["rom_id"] = rom_hex;

  return true;
}

}  // namespace orchard::sensors

// Self-register so the driver shows up in the registry without any
// central #include / wiring.
static orchard::sensors::AutoRegister<orchard::sensors::DS18B20Sensor>
    _ds18b20_register;
