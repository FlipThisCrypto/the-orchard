// SPDX-License-Identifier: Apache-2.0
//
// USB-serial provisioning console.
//
// Line-oriented commands the Orchard View dashboard uses to set up a
// Tree before it can reach the oracle. Each command is one line, ends
// with \n, and gets a one-line `OK ...` or `ERR ...` reply.
//
// Commands (case-sensitive):
//   PING                       -> "OK pong"
//   STATUS                     -> "OK <json blob>"
//   NODE_ID                    -> "OK <hex>"
//   KEY                        -> "OK <hex 32-byte signing secret>"
//                                 (printed in plaintext over the local
//                                 USB link. NEVER over the network.)
//   WIFI_SET <ssid> <password> -> "OK" (saves and reconnects)
//   WIFI_CLEAR                 -> "OK"
//   ORACLE_SET <url>           -> "OK"
//   SAMPLE_NOW                 -> "OK" (samples sensors + POSTs now)
//   I2C_SCAN                   -> "OK 0xXX 0xYY ..."  (every responding
//                                 I2C address) or "OK (no devices)"
//   GPS_RAW                    -> "OK gps_raw_start" then 3 seconds of
//                                 raw GPS UART bytes streamed inline,
//                                 then "OK gps_raw_end"
//   REBOOT                     -> "OK rebooting"  (then reboots)
//
// Any unknown command -> "ERR unknown".

#pragma once

#include <cstdint>

namespace orchard::net {

void console_begin();

// Drain the console's pending input. As of fw 0.5.0 the console NO LONGER
// reads Serial itself — Improv serial owns the single Serial reader and
// routes non-Improv bytes here via console_feed_byte(). console_loop() is
// kept (a) so the lifecycle in main.cpp reads naturally and (b) as the place
// for any future non-input periodic console work; today it is a no-op. The
// byte routing happens in improv_serial_pump().
void console_loop();

// Feed one received byte to the console's line buffer. Called by the Improv
// serial pump for every byte that is NOT part of an Improv packet. A complete
// line (terminated by '\n') is dispatched exactly as the old console_loop()
// did, so typed commands behave identically. '\r' is ignored; the line buffer
// is bounded to 512 chars.
void console_feed_byte(uint8_t b);

// Optional: callback used to trigger an immediate sample+POST from
// console (lets us avoid a circular dep between this module and main).
using SampleNowFn = void (*)();
void console_set_sample_callback(SampleNowFn fn);

}  // namespace orchard::net
