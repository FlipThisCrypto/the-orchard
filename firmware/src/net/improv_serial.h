// SPDX-License-Identifier: Apache-2.0
//
// Improv Wi-Fi Serial provisioning (https://www.improv-wifi.com/serial/).
//
// Improv is the standard ESP32 provisioning protocol esp-web-tools speaks
// natively. After a browser flashes the board with esp-web-tools, its
// built-in "Connect to Wi-Fi" dialog provisions WiFi over this same USB
// serial line, and — critically — we hand back a claim URL so esp-web-tools
// shows a "Next/Open" button to it. That makes flash -> WiFi -> claim a
// seamless one-page browser flow.
//
// Coexistence with the ASCII console (serial_console.*):
//   Both Improv and the human-typed console commands share ONE Serial
//   stream. There must be exactly one reader. improv_serial owns it: the
//   pump (improv_serial_pump) reads each available byte and feeds it to the
//   Improv parser FIRST. A byte the parser accepts is consumed by Improv; a
//   byte that breaks Improv framing is handed to console_feed_byte() so the
//   typed commands (STATUS, WIFI_SET, ...) still work exactly as before. A
//   human never types the binary "IMPROV" header, so routing is clean.
//
// Improv-set WiFi ONLY sets WiFi credentials. It deliberately does NOT write
// an oracle URL to NVS, because provisioning.cpp treats "oracle URL in NVS"
// as a grandfather signal and would SKIP the claim flow.

#pragma once

#include <cstdint>

namespace orchard::net {

// Initialize Improv serial provisioning. Call from setup() AFTER
// console_begin() (so the banner is printed) and wifi_begin() (so we can
// report the right initial state). Idempotent-ish: only sets up internal
// state; does not touch Serial.begin (the console already opened Serial).
void improv_begin();

// Pump the shared Serial stream. Call this from loop() in place of
// console_loop(): it reads every available byte, routes Improv bytes to the
// Improv parser and everything else to the ASCII console, then services any
// in-flight Improv WiFi connect. This is the SINGLE place that reads Serial.
void improv_serial_pump();

}  // namespace orchard::net
