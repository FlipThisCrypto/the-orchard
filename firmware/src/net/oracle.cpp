// SPDX-License-Identifier: Apache-2.0
#include "oracle.h"

#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "device_reading.h"
#include "identity.h"
#include "timekeeping.h"
#include "version.h"

namespace orchard::net {

namespace {

constexpr const char* kNvsUrl = "oracle_url";

String load_url_() {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/true);
  String u = prefs.getString(kNvsUrl, "");
  prefs.end();
  return u;
}

// Beacon lives at the oracle origin. NVS normally holds the base URL (the
// fleet's provisioned form — oracle_post_reading() appends /readings itself),
// but older units were provisioned with the full …/readings path, so strip
// that suffix when present and derive /beacon from the origin either way.
String beacon_url_from_base_(const String& configured_url) {
  // Strip trailing slashes, then a trailing /readings segment → /beacon
  String base = configured_url;
  while (base.endsWith("/")) {
    base.remove(base.length() - 1);
  }
  const int idx = base.lastIndexOf('/');
  if (idx > (int)strlen("http://")) {
    const String last = base.substring(idx + 1);
    if (last.equalsIgnoreCase("readings")) {
      base = base.substring(0, idx);
    }
  }
  return base + "/beacon";
}

// Throttle beacon refresh: reuse anchor for ~5 minutes.
uint32_t last_beacon_ms_ = 0;
constexpr uint32_t kBeaconRefreshMs = 5 * 60 * 1000;

}  // namespace

String oracle_url() {
  return load_url_();   // NVS value as-is (empty if unset) — for the console/dashboard
}

String oracle_base_url() {
  // Resolved URL the device actually talks to: the NVS override if the
  // operator set one (local-dev / LAN oracle), else the baked default
  // (ADR-0005 §5). Used for posting and for claim-code provisioning.
  String u = load_url_();
  return u.length() ? u : String(ORCHARD_DEFAULT_ORACLE_URL);
}

bool oracle_set_url(const String& url) {
  Preferences prefs;
  prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
  prefs.putString(kNvsUrl, url);
  prefs.end();
  return true;
}

bool oracle_refresh_beacon() {
  if (WiFi.status() != WL_CONNECTED) return false;
  // Same resolution the POST path uses: NVS override if the operator set one,
  // else the baked default (ADR-0005 §5), so an un-overridden Tree still
  // anchors instead of silently signing with the zero placeholder.
  const String configured = oracle_base_url();
  if (configured.length() == 0) return false;

  // Skip if we refreshed recently (unless never).
  if (last_beacon_ms_ != 0 &&
      (millis() - last_beacon_ms_) < kBeaconRefreshMs) {
    return true;
  }

  const String url = beacon_url_from_base_(configured);
  HTTPClient http;
  // Same TLS handling as the POST path / provisioning client: the oracle is
  // Cloudflare-fronted https, and a plain http.begin(url) does not reliably
  // negotiate it. `secure` must outlive the request (HTTPClient borrows it).
  WiFiClientSecure secure;
  bool began;
  if (url.startsWith("https://")) {
    secure.setInsecure();
    began = http.begin(secure, url);
  } else {
    began = http.begin(url);
  }
  if (!began) {
    Serial.printf("[oracle] beacon begin failed: %s\n", url.c_str());
    return false;
  }
  http.setTimeout(5000);
  const int code = http.GET();
  if (code != 200) {
    Serial.printf("[oracle] beacon GET -> %d\n", code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  JsonDocument doc;
  if (deserializeJson(doc, body)) {
    Serial.println("[oracle] beacon JSON parse failed");
    return false;
  }
  const char* anchor = doc["block_anchor"] | "";
  if (strlen(anchor) < 16) {
    Serial.println("[oracle] beacon missing block_anchor");
    return false;
  }
  orchard::set_block_anchor(anchor);
  last_beacon_ms_ = millis();
  Serial.printf("[oracle] beacon anchor=%s height=%ld source=%s\n",
                anchor,
                static_cast<long>(doc["block_height"] | 0),
                doc["source"] | "?");
  return true;
}

bool oracle_post_reading(JsonDocument& payload) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[oracle] WiFi not connected; skipping POST");
    return false;
  }
  String url = oracle_base_url();  // NVS override or the baked default
  if (url.length() == 0) {
    Serial.println("[oracle] no URL configured; skipping POST");
    return false;
  }
  // The reading-ingest endpoint is POST <base>/readings. oracle_base_url() is
  // only the host root, so append the path (Cloudflare blocks the bare root, so
  // a missing path here is a hard 403 — see fw 0.5.0 field reports). Strip any
  // trailing slash first so we never produce "//readings".
  while (url.endsWith("/")) url.remove(url.length() - 1);
  url += "/readings";

  // SPEC §4.2: refresh block_anchor before signing the device reading.
  oracle_refresh_beacon();

  // Add identity fields.
  payload["schema"]  = 1;  // ADR-0006/T14: payload format version (start at 1)
  payload["node_id"] = identity::node_id_hex();
  payload["fw"]      = orchard::kFirmwareVersion;

  // ADR-0003/0007: tell the oracle which key signs our readings.
  //
  // Without this a Tree signs every reading and never says what verifies them,
  // so the oracle cannot populate node.device_pubkey, the publisher refuses the
  // node as unverifiable, and NOTHING that Tree measures can ever be published.
  // That is what happened to the first signing Tree (2026-08-08): its key had
  // to be recovered off-chain from its own ECDSA signatures and written into
  // the production database by hand. Elegant once; not a fleet procedure, and
  // not something that belongs in a provenance chain.
  //
  // TOP LEVEL, deliberately — not inside device_reading:
  //   * it rides inside the HMAC'd body, so only the holder of this Tree's
  //     device secret can assert it;
  //   * it does NOT change the secp256r1-signed canonical bytes, so signatures
  //     stay valid and the publisher's SPEC field-stripping is unaffected;
  //   * the key reaches the chain once, in the node: card, rather than being
  //     repeated inside every published reading.
  // The oracle writes it once and never rotates it, so a wrong value cannot
  // silently replace a good one — and a wrong value fails signature checks
  // loudly rather than passing quietly.
  payload["device_pubkey"] = identity::p256_pubkey_hex();
  // Monotonic per-boot placeholder (not wall-clock). Wall-clock is preferred:
  // attach_device_reading() below overwrites this with epoch millis whenever
  // GPS UTC or a synced system clock is available. A caller-supplied ts_ms is
  // never clobbered.
  if (!payload["ts_ms"].is<int64_t>() && !payload["ts_ms"].is<int>()) {
    payload["ts_ms"] = static_cast<int64_t>(millis());
  }
  payload["seq"]     = identity::next_seq();  // replay protection — inside the
                                              // HMAC'd body, so it can't be
                                              // bumped on a captured packet
  // D6/T6: real UTC epoch seconds once SNTP has synced (GPS UTC, when a
  // fix exists, is also in sensors.gps.utc). Omitted until first sync so a
  // pre-sync reading never carries a bogus 1970 timestamp. The oracle's
  // freshness guard reads this field, so it must survive the merge.
  const uint32_t epoch = utc_now();
  if (epoch > 0) {
    payload["ts"] = epoch;
  }

  // ADR-0003: attach secp256r1-signed SPEC reading for DataLayer publish.
  // Runs last so the signed reading covers the final metric set and can
  // normalise ts_ms to wall-clock. Failure is non-fatal — the sensors blob +
  // HMAC transport (and therefore the live ingest path) still work.
  orchard::attach_device_reading(payload);

  String body;
  serializeJson(payload, body);

  // Sign the canonical body. The oracle recomputes HMAC over the
  // received body and compares.
  uint8_t sig[32];
  identity::hmac_sha256(reinterpret_cast<const uint8_t*>(body.c_str()),
                        body.length(), sig);
  String sig_hex = identity::to_hex(sig, sizeof(sig));

  HTTPClient http;
  // https gets a TLS client with cert verification disabled — the oracle is
  // Cloudflare-fronted and every body is signature-protected (HMAC +
  // secp256r1), so transport auth isn't what guarantees integrity. `secure`
  // must outlive the request (HTTPClient borrows it). Pinning the CA is a
  // hardening follow-up (T10 track).
  WiFiClientSecure secure;
  bool began;
  if (url.startsWith("https://")) {
    secure.setInsecure();
    began = http.begin(secure, url);
  } else {
    began = http.begin(url);
  }
  if (!began) {
    Serial.printf("[oracle] http.begin failed for %s\n", url.c_str());
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Orchard-Node", identity::node_id_hex());
  http.addHeader("X-Orchard-Sig",  sig_hex);
  http.setTimeout(10000);

  const int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("[oracle] POST error: %s\n",
                  HTTPClient::errorToString(code).c_str());
    http.end();
    return false;
  }
  Serial.printf("[oracle] POST -> %d (%u bytes)\n", code, (unsigned)body.length());
  if (code < 200 || code >= 300) {
    String resp = http.getString();
    if (resp.length() > 160) resp = resp.substring(0, 160) + "...";
    Serial.printf("[oracle] body: %s\n", resp.c_str());
  }
  http.end();
  return code >= 200 && code < 300;
}

}  // namespace orchard::net
