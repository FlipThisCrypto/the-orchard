// SPDX-License-Identifier: Apache-2.0
#include "sensor.h"

#include <Arduino.h>

namespace orchard::sensors {

SensorRegistry& SensorRegistry::instance() {
  static SensorRegistry r;
  return r;
}

void SensorRegistry::add(std::unique_ptr<Sensor> s) {
  sensors_.push_back(std::move(s));
}

void SensorRegistry::begin_all() {
  for (auto& s : sensors_) {
    const bool ok = s->begin();
    s->set_active(ok);
    Serial.printf("[sensors] %-12s bus=%d active=%s\n",
                  s->name(),
                  static_cast<int>(s->bus_type()),
                  ok ? "yes" : "no");
  }
}

size_t SensorRegistry::active_count() const {
  size_t n = 0;
  for (const auto& s : sensors_) {
    if (s->is_active()) ++n;
  }
  return n;
}

void SensorRegistry::sample_all(JsonObject parent) {
  for (auto& s : sensors_) {
    if (!s->is_active()) continue;
    JsonObject child = parent[s->name()].to<JsonObject>();
    // A sensor only stays in the payload if read() produced a VALID reading.
    // A failed/absent read is omitted entirely — not zero-filled, not tagged
    // with an error — so the oracle and dashboard only ever see sensors that
    // are genuinely present and reporting real data (ADR-0006). Reporting
    // phantom values would undermine the whole "verifiable data" premise.
    if (!s->read(child)) {
      parent.remove(s->name());
    }
  }
}

}  // namespace orchard::sensors
