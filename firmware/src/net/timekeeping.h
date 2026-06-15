// SPDX-License-Identifier: Apache-2.0
//
// SNTP wall-clock time (HANDOVER D6 / T6).
//
// After WiFi connects we start the ESP32's built-in SNTP client so each
// reading can carry a real UTC `ts` (epoch seconds) even when the Tree has
// no GPS fix. This is purely for data quality + an optional oracle-side
// freshness check; replay protection is the `seq` counter, not the clock.
//
// GPS UTC, when a fix exists, continues to be reported in the gps sensor
// block (`sensors.gps.utc`) — a verifier can prefer that.

#pragma once

#include <cstdint>

namespace orchard::net {

// Arm the SNTP client (UTC, two public servers). Idempotent — safe to call
// on every WiFi-connect transition; only the first call starts it.
void time_sync_begin();

// True once the system clock holds a plausible real-world time (SNTP synced).
bool time_is_synced();

// Real UTC epoch seconds once synced, else 0. Callers include `ts` in the
// payload only when this is non-zero, so pre-sync readings simply omit it
// (old behavior) rather than reporting a bogus 1970 timestamp.
uint32_t utc_now();

}  // namespace orchard::net
