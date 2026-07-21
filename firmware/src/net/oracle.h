// SPDX-License-Identifier: Apache-2.0
//
// Oracle client: POSTs a signed JSON reading to the configured oracle URL.

#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

namespace orchard::net {

// Oracle URL is stored in NVS (provisioned via serial). Empty == unset.
String oracle_url();
bool   oracle_set_url(const String& url);

// Build the canonical payload and POST it. Returns true on 2xx.
// Caller fills `payload` with sensor data; this function adds node_id,
// firmware version, and timestamp, then signs the body.
bool oracle_post_reading(JsonDocument& payload);

// GET /beacon from the oracle origin and cache block_anchor for the next
// device_reading signature (SPEC §4.2 anti-backdate). Returns true if a
// 16-hex anchor was obtained and applied. Soft-fails (keeps prior/zero).
bool oracle_refresh_beacon();

}  // namespace orchard::net
