// SPDX-License-Identifier: Apache-2.0
//
// Build-time defaults for The Orchard Tree firmware.
//
// Runtime config (WiFi creds, oracle URL, device identity) is stored in
// NVS via the `Preferences` library and provisioned over the USB-serial
// console — never compiled in.

#pragma once

// How often to sample sensors and POST to the oracle, in milliseconds.
#ifndef ORCHARD_SAMPLE_INTERVAL_MS
#define ORCHARD_SAMPLE_INTERVAL_MS  60000  // 60 seconds
#endif

// How long to wait for WiFi to come up before going back to soft-AP /
// serial-provisioning mode.
#ifndef ORCHARD_WIFI_CONNECT_TIMEOUT_MS
#define ORCHARD_WIFI_CONNECT_TIMEOUT_MS  20000
#endif

// HTTP server port on the device. Exposes /health and /ota.
#ifndef ORCHARD_DEVICE_HTTP_PORT
#define ORCHARD_DEVICE_HTTP_PORT  80
#endif

// USB-serial console baud (matches monitor_speed in platformio.ini).
#ifndef ORCHARD_CONSOLE_BAUD
#define ORCHARD_CONSOLE_BAUD  115200
#endif

// GPS UART baud — NEO-6M/7M/8M default to 9600.
#ifndef ORCHARD_GPS_BAUD
#define ORCHARD_GPS_BAUD  9600
#endif

// Soft-AP fallback SSID prefix. Suffix = last 4 hex of node_id.
#ifndef ORCHARD_AP_SSID_PREFIX
#define ORCHARD_AP_SSID_PREFIX  "OrchardTree-"
#endif

// Length of the per-device random soft-AP password.
//
// We do NOT define a default password here. A shared default across
// every Tree in the fleet would be a same-password-everywhere attack
// (anyone who reads this header can join any Tree's AP). Instead the
// password is generated per-device on first boot by
// orchard::identity::ap_password(), persisted in NVS, and printed to
// the serial console the one time it's generated so the operator can
// note it.
#ifndef ORCHARD_AP_PASSWORD_LEN
#define ORCHARD_AP_PASSWORD_LEN  12
#endif

// NVS namespace key.
#ifndef ORCHARD_NVS_NAMESPACE
#define ORCHARD_NVS_NAMESPACE  "orchard"
#endif

// Default oracle base URL, baked at flash time (ADR-0005 §5). A freshly
// flashed Tree posts here and runs claim-code provisioning against it
// without the operator configuring a URL. The dashboard's local-dev flow
// still overrides this by writing oracle_url into NVS (e.g. a LAN oracle).
#ifndef ORCHARD_DEFAULT_ORACLE_URL
#define ORCHARD_DEFAULT_ORACLE_URL  "https://oracle.theorchard.network"
#endif

// How often the unprovisioned Tree re-announces + polls the claim endpoint.
#ifndef ORCHARD_PROVISION_POLL_MS
#define ORCHARD_PROVISION_POLL_MS  15000  // 15 seconds
#endif
