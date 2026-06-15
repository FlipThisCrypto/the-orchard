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

  // Try to bind a device now, but DON'T deactivate if the bus is empty:
  // the probe may power up slightly after us, or the 1-Wire bus can glitch
  // during the boot WiFi-association current spike, and a one-shot scan would
  // then miss a really-present sensor forever. Instead we stay active and
  // re-probe from read(); read() omits the sensor until a real device answers
  // with a valid temperature (ADR-0006). Always returning true keeps the
  // registry sampling us, which is what drives the re-probe.
  if (!probe_()) {
    Serial.println(
        "[ds18b20] no device on the 1-Wire bus yet — will keep re-probing. "
        "If a probe IS wired, check the 4.7k pull-up between DATA and VCC, "
        "and that DATA is on the configured GPIO (see pins.h).");
  }
  return true;
}

bool DS18B20Sensor::probe_() {
  device_count_ = dt_.getDeviceCount();
  if (device_count_ <= 0) { device_count_ = 0; return false; }
  if (!dt_.getAddress(rom_, 0)) { device_count_ = 0; return false; }

  // 12-bit (1/16 °C) resolution gives ~0.0625 °C steps with a ~750 ms
  // conversion time — fine at our 60-second sample cadence.
  dt_.setResolution(rom_, 12);

  // Non-blocking conversions. By default DallasTemperature blocks inside
  // requestTemperatures() for the full ~750 ms conversion; that block
  // overlapping the boot WiFi handshake browned out the S3 (LOG 2026-06-02).
  // setWaitForConversion(false) makes read() return in microseconds: we
  // request a conversion now and read its result on the NEXT sample tick.
  dt_.setWaitForConversion(false);
  dt_.requestTemperatures();
  return true;
}

bool DS18B20Sensor::read(JsonObject out) {
  // No device bound yet? Re-probe (handles slow power-up / boot bus glitch /
  // a probe plugged in after boot). probe_() kicks the first conversion, so
  // there's nothing valid to report on THIS tick either way — omit and report
  // on the next one once a device is bound and its conversion is done.
  if (device_count_ <= 0) {
    probe_();
    return false;
  }

  // Read the conversion requested on the previous tick, then kick the next.
  const float t = dt_.getTempC(rom_);
  dt_.requestTemperatures();

  // DallasTemperature reports DEVICE_DISCONNECTED_C (-127.0) when the chip
  // doesn't respond. Omit the sample (never publish the sentinel as a real
  // -127 °C) and force a re-probe next tick in case it comes back.
  if (t == DEVICE_DISCONNECTED_C) {
    device_count_ = 0;
    Serial.println("[ds18b20] chip not responding; dropping sample, re-probing next tick");
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
