// SPDX-License-Identifier: Apache-2.0
#pragma once

// Compressed SEC1 P-256 public key (33 bytes, hex, 66 chars) for the
// project OTA release key. Empty string = no release key baked in yet:
// OTA signature verification is a no-op / warn-only until OWNER generates
// the keypair and this constant is set (docs/security/SIGNED_OTA.md).
//
// This is a PUBLIC key. Never put the private signing key in firmware.
#ifndef ORCHARD_OTA_RELEASE_PUBKEY_HEX
#define ORCHARD_OTA_RELEASE_PUBKEY_HEX ""
#endif
