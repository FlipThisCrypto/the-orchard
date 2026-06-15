// SPDX-License-Identifier: Apache-2.0
#include "timekeeping.h"

#include <Arduino.h>
#include <ctime>

namespace orchard::net {

namespace {
bool armed_ = false;
// Sanity floor (2023-11-14T22:13:20Z). `time(nullptr)` returns a near-zero
// value until SNTP sets the clock, so anything past this means really synced.
constexpr uint32_t kSaneEpoch = 1700000000UL;
}  // namespace

void time_sync_begin() {
  if (armed_) return;
  // UTC: zero GMT offset, zero DST. Two public servers for redundancy
  // (per HANDOVER D6). The SNTP client runs in the background and sets the
  // system clock within a few seconds of the first WiFi connection.
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
  armed_ = true;
  Serial.println("[time] SNTP started (UTC): pool.ntp.org, time.cloudflare.com");
}

bool time_is_synced() {
  return static_cast<uint32_t>(time(nullptr)) >= kSaneEpoch;
}

uint32_t utc_now() {
  const uint32_t now = static_cast<uint32_t>(time(nullptr));
  return now >= kSaneEpoch ? now : 0;
}

}  // namespace orchard::net
