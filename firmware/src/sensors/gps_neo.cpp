// SPDX-License-Identifier: Apache-2.0
#include "gps_neo.h"

#include <Arduino.h>
#include <HardwareSerial.h>
#include "config.h"
#include "pins.h"

namespace orchard::sensors {

// We claim UART1 for the GPS. UART0 stays free for USB-CDC console;
// UART2 is reserved for PMS5003 later.
static HardwareSerial gps_uart(1);

// Baud rates to try in order. 9600 first (u-blox factory default for
// every NEO-6M/7M/8M I've seen direct from u-blox), then 38400 (the
// common HiLetgo / clone preconfigure), then the rest. NEO-M8 series
// can ship at 115200; very old or repurposed boards at 4800.
namespace {
constexpr uint32_t kCommonBauds[] = {9600, 38400, 19200, 57600, 115200, 4800};
constexpr size_t kCommonBaudCount =
    sizeof(kCommonBauds) / sizeof(kCommonBauds[0]);

// Per-rate listen window. NEO modules emit one full sentence cycle
// (GGA/GSA/GSV/RMC/VTG) per second by default, so ~1.5s is enough to
// see several `*XX\r\n` checksum frames at the right baud — and bail
// fast at the wrong baud.
constexpr uint32_t kBaudProbeMs = 1500;
}  // namespace

GpsNeoSensor::ProbeResult GpsNeoSensor::try_baud_probe_(uint32_t baud) {
  gps_uart.end();
  gps_uart.begin(baud, SERIAL_8N1,
                 ORCHARD_PIN_GPS_RX, ORCHARD_PIN_GPS_TX);
  delay(50);                                  // let UART stabilize
  while (gps_uart.available()) gps_uart.read();  // drain stale buffer

  // Track delta on TinyGPS++'s passed/failed counters across THIS
  // probe — the underlying counters are cumulative across all
  // begin() attempts. We watch BOTH counters because they encode
  // different diagnoses:
  //   passed > 0  → bytes arrive AND validate, baud is right
  //   failed > 0  → bytes arrive in sentence shape but checksum
  //                 doesn't match, which means either a flipped bit
  //                 in transit (loose wire, EMI, weak driver) OR
  //                 something close-but-not-quite (rare baud aliasing)
  //   both = 0    → no sentence framing at all, this baud is wrong
  // The previous version only watched `passed` and so couldn't tell
  // "wrong baud" from "right baud but corrupt line" — that masked a
  // hardware-side regression (e.g. a wire jiggled loose during USB
  // cable manipulation for the flash) as a software regression.
  const uint32_t start_passed = gps_.passedChecksum();
  const uint32_t start_failed = gps_.failedChecksum();

  const uint32_t end_ms = millis() + kBaudProbeMs;
  while (millis() < end_ms) {
    while (gps_uart.available()) {
      gps_.encode(gps_uart.read());
    }
    delay(2);
  }
  return {
    gps_.passedChecksum() - start_passed,
    gps_.failedChecksum() - start_failed,
  };
}

bool GpsNeoSensor::begin() {
  // Default path: single begin() at ORCHARD_GPS_BAUD (9600, u-blox
  // factory). Matches firmware 0.1.0 verbatim — which is what was
  // working on the live WROOM-32U Tree before the auto-baud probe
  // was added in 0.3.0 (commit 08db0ac).
  //
  // The 0.3.0 probe was a real win for HiLetgo-style clones at 38400,
  // but its 6-cycle end()/begin()/drain churn appeared to leave the
  // ESP32's UART peripheral in a degraded state on the live WROOM-32U
  // (likely arduino-esp32 framework behavior change between the
  // version 0.1.0 was built against and the version 0.4.x fetched on
  // rebuild). The probe locked at 4800 — exactly half of 9600, where
  // half-rate aliasing made corrupted-byte framing accidentally look
  // higher than at the actual 9600 transmit rate.
  //
  // To re-enable the probe for a HiLetgo-style clone at 38400, build
  // with -D ORCHARD_GPS_AUTOBAUD=1. Default keeps the single-begin
  // path that works on factory NEO modules.
  //
  // History: LOG.md 2026-05-29 entry + commit 08db0ac (probe added).
#ifndef ORCHARD_GPS_AUTOBAUD
#define ORCHARD_GPS_AUTOBAUD 0
#endif

#if ORCHARD_GPS_AUTOBAUD
  // Opt-in: u-blox factory is 9600 but a lot
  // of clone modules (HiLetgo, generic AliExpress) ship preconfigured
  // for 38400 or other rates, producing garbled output at 9600.
  //
  // Lock strategy:
  //   1. The rate with the highest `passed` count wins (clean lock).
  //   2. If no rate has `passed >= 2` but some rate has framing
  //      activity (failed > 0), lock at the rate with the most TOTAL
  //      framing — that's almost certainly the correct baud, but
  //      something downstream is corrupting bytes (hardware issue).
  //      We surface that distinction loudly in the boot log so the
  //      operator immediately knows it's a wiring/EMI check, not a
  //      firmware change.
  //   3. Nothing has framing at all → default to 9600 with a generic
  //      "is the GPS even talking?" warning.
  uint32_t best_baud = 0;
  ProbeResult best{0, 0};
  for (size_t i = 0; i < kCommonBaudCount; ++i) {
    const uint32_t baud = kCommonBauds[i];
    const ProbeResult r = try_baud_probe_(baud);  // NOLINT(misc-include-cleaner)
    Serial.printf("[gps] probe baud=%6u: passed=%u failed=%u\n",
                  baud, r.passed, r.failed);
    // Prefer the rate with the most clean sentences. If that's tied
    // (likely all zero), the rate with the most TOTAL framing wins.
    const bool better =
        (r.passed > best.passed) ||
        (r.passed == best.passed &&
         (r.passed + r.failed) > (best.passed + best.failed));
    if (better) {
      best_baud = baud;
      best = r;
    }
  }

  if (best.passed >= 2) {
    // Clean lock.
    gps_uart.end();
    gps_uart.begin(best_baud, SERIAL_8N1,
                   ORCHARD_PIN_GPS_RX, ORCHARD_PIN_GPS_TX);
    detected_baud_ = best_baud;
    Serial.printf("[gps] locked at %u baud (clean NMEA)\n", detected_baud_);
    return true;
  }

  if (best.failed > 0) {
    // Framing at this baud, but every sentence corrupt → almost
    // certainly the right baud + a HARDWARE integrity problem
    // (loose wire, cold solder joint, EMI from GPS antenna onto the
    // data line, marginal 3.3V brown-out under wifi + sample load).
    // Lock at the best rate so the dashboard tile shows real data
    // when sentences DO arrive clean — but surface the diagnosis.
    gps_uart.end();
    gps_uart.begin(best_baud, SERIAL_8N1,
                   ORCHARD_PIN_GPS_RX, ORCHARD_PIN_GPS_TX);
    detected_baud_ = best_baud;
    Serial.printf(
      "[gps] WARN: best baud %u shows NMEA framing (failed=%u) but "
      "ZERO clean checksums. This is a DATA INTEGRITY issue, not a "
      "baud mismatch. Most likely: (a) a wire jiggled during USB "
      "cable manipulation — re-seat the GPS connector, (b) cold "
      "solder joint on the breakout — wiggle each wire while watching "
      "GPS_RAW, (c) EMI from the GPS antenna onto the data line — "
      "route it farther from the data wires, (d) marginal 3.3V under "
      "wifi+sample load — try a powered USB hub.\n", best_baud, best.failed);
    return true;
  }

  // Nothing produced any framing. Could mean: wires disconnected, GPS
  // unpowered, GPS in UBX-binary-only mode, or the data wire is OPEN
  // (no continuity at all). Fall back to 9600 so the UART is in a
  // sane state and any future bytes get parsed. Surface baud=0 so the
  // dashboard tile shows the operator the difference between "fix in
  // progress" and "module silent."
  gps_uart.end();
  gps_uart.begin(ORCHARD_GPS_BAUD, SERIAL_8N1,
                 ORCHARD_PIN_GPS_RX, ORCHARD_PIN_GPS_TX);
  Serial.println("[gps] WARN: no NMEA framing at any tried baud rate. "
                 "Check wiring (GPS TX -> GPIO 18), power (GPS VCC, GND), "
                 "antenna, and that the module isn't in UBX-only mode.");
  detected_baud_ = 0;
  return true;
#endif  // ORCHARD_GPS_AUTOBAUD

  // 0.1.0 path: single begin at the configured baud, no probe.
  gps_uart.begin(ORCHARD_GPS_BAUD, SERIAL_8N1,
                 ORCHARD_PIN_GPS_RX, ORCHARD_PIN_GPS_TX);
  detected_baud_ = ORCHARD_GPS_BAUD;
  Serial.printf("[gps] init at %u baud (hardcoded, factory default)\n",
                (unsigned)ORCHARD_GPS_BAUD);
  return true;
}

void GpsNeoSensor::pump_uart_() {
  while (gps_uart.available()) {
    gps_.encode(gps_uart.read());
  }
}

bool GpsNeoSensor::read(JsonObject out) {
  pump_uart_();

  // Always report sat count and fix flag, even without a fix.
  out["satellites"]  = gps_.satellites.isValid() ? gps_.satellites.value() : 0;
  out["fix"]         = gps_.location.isValid();
  out["fix_age_ms"]  = gps_.location.isValid() ? gps_.location.age() : 0;
  // Diagnostic surface — lets the dashboard tile distinguish "no fix
  // yet" (baud > 0, sentences > 0, sats > 0) from "module silent"
  // (baud == 0). Useful when an operator's first reaction to GPS not
  // working is "is my wiring right?" — the baud field answers that.
  out["baud"]                  = detected_baud_;
  out["chars_processed"]       = (uint32_t)gps_.charsProcessed();
  out["sentences_passed"]      = (uint32_t)gps_.passedChecksum();
  out["sentences_failed_csum"] = (uint32_t)gps_.failedChecksum();

  if (gps_.location.isValid()) {
    out["lat"] = gps_.location.lat();
    out["lon"] = gps_.location.lng();
  }
  if (gps_.altitude.isValid()) {
    out["alt_m"] = gps_.altitude.meters();
  }
  if (gps_.speed.isValid()) {
    out["speed_kmh"] = gps_.speed.kmph();
  }
  if (gps_.date.isValid() && gps_.time.isValid()) {
    char iso[32];
    snprintf(iso, sizeof(iso),
             "%04d-%02d-%02dT%02d:%02d:%02dZ",
             gps_.date.year(),
             gps_.date.month(),
             gps_.date.day(),
             gps_.time.hour(),
             gps_.time.minute(),
             gps_.time.second());
    out["utc"] = iso;
  }
  return true;
}

void gps_dump_raw(uint32_t duration_ms) {
  // Drain stale bytes so we capture a fresh window.
  while (gps_uart.available()) gps_uart.read();

  const uint32_t end_ms = millis() + duration_ms;
  while (millis() < end_ms) {
    while (gps_uart.available()) {
      Serial.write((char)gps_uart.read());
    }
    delay(2);
  }
}

}  // namespace orchard::sensors

// Self-register.
static orchard::sensors::AutoRegister<orchard::sensors::GpsNeoSensor>
    _gps_register;
