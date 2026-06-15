// SPDX-License-Identifier: Apache-2.0
//
// Maxim/Dallas DS18B20 — waterproof / TO-92 digital temperature probe
// (1-Wire protocol on a single GPIO).
//
// Bus discovery: the driver scans the 1-Wire bus in begin() and binds
// to the first DS18B20 it finds. Multiple probes on the same bus are
// possible — `device_count` is surfaced in the JSON payload so an
// operator can spot when more sensors are physically attached than the
// driver is currently reading — but v1 reports only the first probe.
//
// REQUIRED EXTERNAL PART: a 4.7 kΩ pull-up resistor between the data
// pin (see ORCHARD_PIN_DS18B20_DATA in pins.h) and 3.3V. Without it
// the 1-Wire bus floats and the chip never responds.

#pragma once

#include "sensor.h"

#include <DallasTemperature.h>
#include <OneWire.h>

namespace orchard::sensors {

class DS18B20Sensor : public Sensor {
 public:
  const char* name() const override { return "ds18b20"; }
  BusType bus_type() const override { return BusType::kDigital; }

  bool begin() override;
  bool read(JsonObject out) override;

 private:
  // (Re)scan the 1-Wire bus and bind the first device. Returns true if a
  // device is now bound. Called from begin() and again from read() whenever
  // we don't yet have a device, so a sensor that powers up late (or after a
  // boot bus glitch) is still picked up rather than missed forever.
  bool probe_();

  // OneWire and DallasTemperature both default-construct happily; the
  // bus pin is bound in begin().
  OneWire wire_;
  DallasTemperature dt_;
  DeviceAddress rom_ = {0};   // 8-byte ROM id of the bound chip
  int device_count_ = 0;      // > 0 == a device is currently bound (rom_ valid)
};

}  // namespace orchard::sensors
