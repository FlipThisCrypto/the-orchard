// SPDX-License-Identifier: Apache-2.0
#include "improv_serial.h"

#include <Arduino.h>

#include <string>
#include <vector>

#include "config.h"
#include "identity.h"
#include "improv.h"
#include "serial_console.h"
#include "version.h"
#include "wifi_mgr.h"

// Per-build board identifier (e.g. "freenove-s3"), from -D in platformio.ini.
// Same fallback the console uses so GET_DEVICE_INFO always has a valid value.
#ifndef ORCHARD_BOARD_HINT
#define ORCHARD_BOARD_HINT "generic"
#endif

namespace orchard::net {

namespace {

// --- Improv packet framing constants (mirror improv.h) ---------------------
// Header is the 6 ASCII bytes "IMPROV" + a version byte; position 7 is the
// packet type, position 8 the data length, then the data, then 1 checksum
// byte. The header is exactly 9 bytes (indices 0..8) before any data.
constexpr uint8_t kHeaderLen = 9;
constexpr uint8_t kVersion   = improv::IMPROV_SERIAL_VERSION;  // 0x01

// Bounded scratch for one inbound packet. Max sane Improv packet is the
// 9-byte header + 255-byte data field + 1 checksum = 265 bytes. Round up.
uint8_t  rx_buffer_[280];
size_t   rx_pos_ = 0;

// --- Connect-wait tunables -------------------------------------------------
// WIFI_SETTINGS hands off to wifi_set_credentials() (which is non-blocking and
// lets wifi_loop() drive the actual association on its 250ms cadence). We then
// spin here until wifi_connected(), bounded so we never starve the watchdog:
// short delay() slices like the rest of the firmware uses, up to ~15s total.
constexpr uint32_t kConnectTimeoutMs   = 15000;
constexpr uint32_t kConnectPollDelayMs = 100;

// ---------------------------------------------------------------------------
// Low-level packet TX. Frames a data field as a full Improv serial packet:
//   "IMPROV" | version | type | len | data... | checksum
// where checksum = (sum of every preceding byte) mod 256. Writes it to
// Serial in one go and terminates with "\r\n" (esp-web-tools tolerates the
// trailing newline and it keeps the line readable in a raw monitor).
// ---------------------------------------------------------------------------
void send_packet_(improv::ImprovSerialType type, const std::vector<uint8_t>& data) {
  std::vector<uint8_t> pkt;
  pkt.reserve(kHeaderLen + data.size() + 1);
  pkt.push_back('I');
  pkt.push_back('M');
  pkt.push_back('P');
  pkt.push_back('R');
  pkt.push_back('O');
  pkt.push_back('V');
  pkt.push_back(kVersion);
  pkt.push_back(static_cast<uint8_t>(type));
  pkt.push_back(static_cast<uint8_t>(data.size()));
  pkt.insert(pkt.end(), data.begin(), data.end());

  uint32_t checksum = 0;
  for (uint8_t b : pkt) checksum += b;
  pkt.push_back(static_cast<uint8_t>(checksum & 0xFF));

  Serial.write(pkt.data(), pkt.size());
  Serial.write('\r');
  Serial.write('\n');
}

// Send the current device state (TYPE_CURRENT_STATE, 1 data byte).
void send_current_state_(improv::State state) {
  send_packet_(improv::TYPE_CURRENT_STATE, {static_cast<uint8_t>(state)});
}

// Send an error (TYPE_ERROR_STATE, 1 data byte).
void send_error_(improv::Error error) {
  send_packet_(improv::TYPE_ERROR_STATE,
               {static_cast<uint8_t>(error)});
}

// Send an RPC response. build_rpc_response() packs `datum` as a list of
// length-prefixed strings into the RPC-response *data field*
// (command | field_len | [str_len|str_bytes]...), and we frame that as a
// TYPE_RPC_RESPONSE packet (which adds its own outer checksum). We pass
// add_checksum=false to build_rpc_response so there is exactly ONE checksum
// (the packet checksum send_packet_ appends), per the Improv spec.
void send_rpc_response_(improv::Command command,
                        const std::vector<std::string>& datum) {
  std::vector<uint8_t> data =
      improv::build_rpc_response(command, datum, /*add_checksum=*/false);
  send_packet_(improv::TYPE_RPC_RESPONSE, data);
}

// The device's current Improv state, derived from WiFi. Orchard needs no
// authorization, so we are PROVISIONED when connected and READY (mapped to
// the spec's "authorized" steady state) otherwise — never
// AWAITING_AUTHORIZATION.
improv::State current_state_() {
  return wifi_connected() ? improv::STATE_PROVISIONED
                          : improv::STATE_AUTHORIZED;
}

// The claim URL esp-web-tools opens after a successful provision. Format is
// fixed by the product spec:
//   https://oracle.theorchard.network/claim?code=<CLAIM_CODE>
// <CLAIM_CODE> is the RAW claim code (identity::claim_code(), 8 Crockford
// chars) — NOT the hyphenated display form (provisioning.cpp's grouped_code).
// We hard-code the host rather than reading oracle_base_url() so an operator's
// LAN-oracle NVS override can't redirect the browser claim flow off the
// production claim page.
std::string claim_url_() {
  std::string url = "https://oracle.theorchard.network/claim?code=";
  url += identity::claim_code().c_str();
  return url;
}

// Handle a fully-decoded, checksum-verified RPC command. Returns true if the
// command was recognized (false -> caller emits ERROR_UNKNOWN_RPC).
bool on_command_(const improv::ImprovCommand& cmd) {
  switch (cmd.command) {
    case improv::GET_CURRENT_STATE: {
      // Report state only. Per the spec the redirect URL travels on the
      // WIFI_SETTINGS RPC *result* (esp-web-tools correlates an RPC response
      // to the command it sent), so we do NOT emit a WIFI_SETTINGS-typed
      // response here — that would be a response to a command the host never
      // sent. A host that wants the URL provisions via WIFI_SETTINGS.
      send_current_state_(current_state_());
      return true;
    }

    case improv::GET_DEVICE_INFO: {
      // Four length-prefixed strings, in spec order:
      //   firmware name, firmware version, chip/hardware variant, device name.
      const std::vector<std::string> info = {
          "Orchard Tree",
          std::string(orchard::kFirmwareVersion),
          std::string(ORCHARD_BOARD_HINT),
          "Orchard Tree " + std::string(identity::node_id_hex().c_str()),
      };
      send_rpc_response_(improv::GET_DEVICE_INFO, info);
      return true;
    }

    case improv::WIFI_SETTINGS: {
      if (cmd.ssid.empty()) {
        send_error_(improv::ERROR_INVALID_RPC);
        return true;
      }
      // Set state PROVISIONING, hand creds to wifi_mgr, then wait (bounded)
      // for the association to come up. wifi_set_credentials() is
      // non-blocking and resets the reconnect throttle so wifi_loop() picks
      // the new creds up immediately; we drive wifi_loop() inside the wait so
      // the connect actually progresses while we block here.
      send_current_state_(improv::STATE_PROVISIONING);

      wifi_set_credentials(String(cmd.ssid.c_str()),
                           String(cmd.password.c_str()));

      const uint32_t start = millis();
      while (!wifi_connected() &&
             (millis() - start) < kConnectTimeoutMs) {
        wifi_loop();             // advance the async connect state machine
        delay(kConnectPollDelayMs);  // short slice: feeds the watchdog/idle task
      }

      if (wifi_connected()) {
        // Success: per the Improv spec the WIFI_SETTINGS RPC result carries
        // the list of redirect URL(s). We send exactly one — the claim URL —
        // then flip to PROVISIONED. esp-web-tools renders a button to it.
        Serial.printf("[improv] provisioned; claim url: %s\n",
                      claim_url_().c_str());
        send_rpc_response_(improv::WIFI_SETTINGS, {claim_url_()});
        send_current_state_(improv::STATE_PROVISIONED);
      } else {
        Serial.println("[improv] WIFI_SETTINGS: connect timeout");
        send_error_(improv::ERROR_UNABLE_TO_CONNECT);
      }
      return true;
    }

    default:
      return false;
  }
}

// Reset the Improv receive cursor. The buffered prefix bytes (if any) are the
// caller's responsibility to replay before this is called.
inline void reset_rx_() { rx_pos_ = 0; }

// Feed one byte to the Improv state machine, with full console coexistence.
//
// We append the byte to rx_buffer_ at rx_pos_ and ask the reference parser
// whether it belongs to a valid Improv packet at this position:
//
//   * returns true  -> byte is a valid (partial) Improv byte. Keep it; bump
//                      rx_pos_. (If this was the checksum byte the parser
//                      already invoked on_command_ before returning... no —
//                      see below: completion returns false.)
//   * returns false -> the byte does NOT continue a valid Improv packet.
//                      Two cases:
//                        (a) Packet COMPLETED (or failed its checksum): the
//                            header had already matched ("IMPROV"+version, so
//                            rx_pos_ >= 7). The parser fired the command/error
//                            callback for us at the checksum byte. These bytes
//                            were genuinely Improv — DO NOT replay them. Reset.
//                        (b) NOT Improv after all: framing broke before the
//                            header completed (rx_pos_ < 7, i.e. version not
//                            yet confirmed). Every buffered byte plus this one
//                            was ordinary console input — replay them all to
//                            the console so no typed byte is lost. Reset.
//
// The 7-byte header ("IMPROV"+version) is the commit point: only once those
// match are we sure the stream is Improv. A human never types that exact
// binary prefix, so case (b) covers all typed input (including words that
// merely start with 'I').
void improv_feed_byte_(uint8_t byte) {
  // Guard the scratch buffer; a malformed stream that somehow advanced rx_pos_
  // past the buffer just gets reset (and the byte treated as console input).
  if (rx_pos_ >= sizeof(rx_buffer_)) {
    reset_rx_();
  }

  rx_buffer_[rx_pos_] = byte;

  bool valid = improv::parse_improv_serial_byte(
      rx_pos_, byte, rx_buffer_,
      [](improv::ImprovCommand cmd) -> bool {
        if (cmd.command == improv::BAD_CHECKSUM) {
          send_error_(improv::ERROR_INVALID_RPC);
          return false;
        }
        if (!on_command_(cmd)) {
          send_error_(improv::ERROR_UNKNOWN_RPC);
          return false;
        }
        return true;
      },
      [](improv::Error err) { send_error_(err); });

  if (valid) {
    ++rx_pos_;
    return;
  }

  // valid == false.
  const bool header_committed = (rx_pos_ >= 7);  // "IMPROV"+version matched
  if (header_committed) {
    // Case (a): a real (or checksum-failed) Improv packet ran to completion.
    // The parser already emitted the response/error. These were Improv bytes.
    reset_rx_();
    return;
  }

  // Case (b): not Improv. Replay every tentatively-buffered prefix byte AND
  // the current breaking byte to the console, in order, so typed commands see
  // their exact input. rx_buffer_[rx_pos_] currently holds `byte`.
  for (size_t i = 0; i <= rx_pos_; ++i) {
    console_feed_byte(rx_buffer_[i]);
  }
  reset_rx_();
}

}  // namespace

void improv_begin() {
  reset_rx_();
  // Announce our initial state so an esp-web-tools session that opens the
  // serial port right after flashing sees where we stand without having to
  // send GET_CURRENT_STATE first. (Harmless to a human watching the monitor:
  // it's one short binary line.)
  send_current_state_(current_state_());
  Serial.println("[improv] ready (Improv Wi-Fi Serial)");
}

void improv_serial_pump() {
  // The SINGLE Serial reader. Every available byte is offered to Improv
  // first; non-Improv bytes fall through to the ASCII console via
  // console_feed_byte() inside improv_feed_byte_().
  while (Serial.available()) {
    improv_feed_byte_(static_cast<uint8_t>(Serial.read()));
  }
}

}  // namespace orchard::net
