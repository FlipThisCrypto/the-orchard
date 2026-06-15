// SPDX-License-Identifier: Apache-2.0
#include "mq135.h"

#include <Arduino.h>
#include <Preferences.h>
#include "config.h"
#include "pins.h"

namespace orchard::sensors {

bool MQ135Sensor::begin() {
  // An analog pin always reads SOME voltage, so an MQ-135 can't be detected
  // present/absent the way I2C/1-Wire sensors can — reading it unconditionally
  // is exactly what made Trees with no MQ-135 wired report phantom air-quality
  // data. So it is OFF by default; the operator opts in per-Tree by setting the
  // NVS "mq135" flag (serial: `SENSOR_MQ135 on`, then reboot). ADR-0006.
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
  const bool present = prefs.getBool("mq135", false);
  prefs.end();
  if (!present) {
    Serial.println("[mq135] disabled — no MQ-135 declared for this Tree. If "
                   "one is wired, run 'SENSOR_MQ135 on' over serial, then reboot.");
    return false;
  }

  pinMode(ORCHARD_PIN_MQ135_ADC, INPUT);
  // 12-bit ADC on ESP32 (0..4095). 11dB attenuation lets us read up to ~3.3V.
  analogReadResolution(12);
  analogSetPinAttenuation(ORCHARD_PIN_MQ135_ADC, ADC_11db);
  return true;
}

bool MQ135Sensor::read(JsonObject out) {
  // Average a small burst to dampen noise.
  constexpr int kSamples = 8;
  uint32_t acc = 0;
  for (int i = 0; i < kSamples; ++i) {
    acc += analogRead(ORCHARD_PIN_MQ135_ADC);
    delay(2);
  }
  const float raw = static_cast<float>(acc) / kSamples;

  // Running baseline (alpha = 0.02 -> ~50-sample window).
  if (!baseline_initialized_) {
    baseline_ = raw;
    baseline_initialized_ = true;
  } else {
    constexpr float kAlpha = 0.02f;
    baseline_ = (1.0f - kAlpha) * baseline_ + kAlpha * raw;
  }

  // Relative deviation from baseline. Positive = worse air quality
  // (more gas -> lower resistance -> higher analog signal on the divider).
  const float deviation = raw - baseline_;
  const float voltage = (raw / 4095.0f) * 3.3f;

  out["adc_raw"]      = raw;
  out["adc_baseline"] = baseline_;
  out["adc_dev"]      = deviation;
  out["voltage_v"]    = voltage;
  return true;
}

}  // namespace orchard::sensors

// Self-register.
static orchard::sensors::AutoRegister<orchard::sensors::MQ135Sensor>
    _mq135_register;
