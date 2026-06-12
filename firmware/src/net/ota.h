// SPDX-License-Identifier: Apache-2.0
//
// HTTP-driven OTA endpoint.
//
// Listens on `ORCHARD_DEVICE_HTTP_PORT` and exposes:
//   GET  /health   -> small JSON with node_id, fw version, uptime
//   POST /ota      -> binary firmware upload (Update API), then reboot
//
// SECURITY (2026-06-09 hardening): /ota uploads are REJECTED unless the
// device was "armed" via the OTA_ARM serial command within a short
// window. Arming needs local USB-serial access, so a remote LAN host can
// no longer push arbitrary firmware (which would be full device takeover
// + key theft). /health stays open (status only).

#pragma once

#include <cstdint>

namespace orchard::net {

void ota_begin();
void ota_loop();  // call from main loop()
void ota_arm(uint32_t window_ms);  // open the OTA upload window (OTA_ARM cmd)

}  // namespace orchard::net
