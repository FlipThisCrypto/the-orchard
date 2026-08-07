// SPDX-License-Identifier: Apache-2.0
//
// Build a device-signed SPEC reading (docs/datalayer/SPEC.md §2.3) and
// attach it to the oracle POST body as `device_reading`.
//
// The oracle HMAC still covers the whole body (transport auth). The
// secp256r1 `sig` on device_reading is the *public* provenance signature
// that DataLayer verifiers check against node:<id>.pubkey (ADR-0003/0007).
//
// Canonical JSON must match orchard_chia.datalayer.schema.canonical_bytes
// byte-for-byte (see testdata/vectors.json "reading_canonical").

#pragma once

#include <ArduinoJson.h>
#include <cstdint>

namespace orchard {

// Optional 16-char lowercase hex block-hash prefix (anti-backdate).
// When empty/null, uses 16 zero hex digits (placeholder until /beacon).
// Cached by the caller between samples if desired.
void set_block_anchor(const char* hex16);

// Build signed device_reading from the sample's sensors object and
// attach it under payload["device_reading"]. Also normalizes top-level
// ts_ms to a wall-clock epoch millis when GPS UTC is available.
//
// Returns true if a signed reading was attached. False if metrics were
// empty (nothing to sign) or signing failed — the legacy sensors blob
// is left intact either way.
bool attach_device_reading(JsonDocument& payload);

}  // namespace orchard
