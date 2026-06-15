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

// Resolved base URL the device talks to: the NVS override if set, else the
// baked default (ORCHARD_DEFAULT_ORACLE_URL). Used by posting + provisioning.
String oracle_base_url();

// Build the canonical payload and POST it. Returns true on 2xx.
// Caller fills `payload` with sensor data; this function adds node_id,
// firmware version, and timestamp, then signs the body.
bool oracle_post_reading(JsonDocument& payload);

}  // namespace orchard::net
