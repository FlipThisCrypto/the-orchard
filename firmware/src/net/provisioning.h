// SPDX-License-Identifier: Apache-2.0
//
// Remote claim-code provisioning client (ADR-0005 / T9).
//
// First boot (NVS wiped / fresh device): the Tree shows a human-readable
// claim code over serial and announces itself (unprovisioned) to the oracle,
// then polls until an operator — authenticated with their wallet in a browser
// — claims that code and binds the Tree to their wallet. Only then does the
// Tree begin normal reading posts.
//
// An already-configured Tree (oracle_url set via the dashboard wizard) is
// grandfathered as provisioned so it never re-enters this flow.

#pragma once

namespace orchard::net {

// Load provisioned state from NVS (grandfathering configured devices).
// Call once in setup() after identity::begin() and wifi_begin().
void provisioning_begin();

// True once the Tree is bound (claimed) — or grandfathered. Gate normal
// sampling/posting on this.
bool is_provisioned();

// While unprovisioned and WiFi is up: re-announce + poll the claim endpoint
// on an interval (printing the claim code for the operator), and flip to
// provisioned when the oracle reports the claim consumed. No-op once
// provisioned. Non-blocking (internally throttled).
void provisioning_loop();

}  // namespace orchard::net
