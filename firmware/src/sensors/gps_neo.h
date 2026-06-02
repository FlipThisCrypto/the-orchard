// SPDX-License-Identifier: Apache-2.0
//
// NEO-6M / 7M / 8M GPS module driver.
//
// Reads NMEA sentences from a UART, parses them with TinyGPSPlus, and
// reports the latest fix when sampled. Continually consumes bytes in
// `read()` so a periodic sample cadence still picks up fresh fixes.

#pragma once

#include "sensor.h"
#include <TinyGPSPlus.h>

namespace orchard::sensors {

class GpsNeoSensor : public Sensor {
 public:
  const char* name() const override { return "gps"; }
  BusType bus_type() const override { return BusType::kUART; }

  bool begin() override;
  bool read(JsonObject out) override;

 private:
  // Probe-window outcome at one baud rate.
  //   passed > 0 → bytes arrive AND validate (right baud, clean)
  //   passed = 0, failed > 0 → bytes arrive in sentence shape but
  //     fail checksum → right baud + data-integrity problem
  //   passed = 0, failed = 0 → no sentence framing → wrong baud
  //     (or wires/power)
  struct ProbeResult {
    uint32_t passed;
    uint32_t failed;
  };

  void pump_uart_();
  ProbeResult try_baud_probe_(uint32_t baud);
  TinyGPSPlus gps_;
  // Baud rate the driver locked onto at startup. 0 = no clean NMEA at
  // any tried rate (driver fell back to 9600 and the dashboard will
  // surface fix=no, satellites=0 indefinitely).
  uint32_t detected_baud_ = 0;
};

// Diagnostic: drain any pending bytes on the GPS UART, then write
// every fresh byte that arrives during `duration_ms` straight to the
// main Serial port. Used by the `GPS_RAW` console command to verify
// the wiring is correct without any TinyGPS++ parsing in the way.
void gps_dump_raw(uint32_t duration_ms);

}  // namespace orchard::sensors
