// SPDX-License-Identifier: BSD-3-Clause
//
// Vendored from the Improv Wi-Fi reference implementation
// (https://github.com/improv-wifi/sdk-serial-protocol — improv.h / improv.cpp).
// Reproduced faithfully under its original BSD-3-Clause license so the
// Orchard Tree firmware can speak the Improv Wi-Fi Serial protocol natively
// (the provisioning protocol esp-web-tools uses). The two reference files
// have been merged into this single header and the implementation made
// `inline` so it can be #included from one translation unit without a
// separate .cpp — Orchard pins all deps and prefers vendoring the small
// reference over adding a library (see platformio.ini "Why exact pins").
//
// One deliberate deviation from upstream: the WIFI_SETTINGS branch of
// parse_improv_data() originally used a C++20 designated initializer; here it
// uses explicit field assignment so the header compiles cleanly under the
// project's -std=gnu++17. Semantics are identical (marked inline below).
//
// Original copyright:
//
//   BSD 3-Clause License
//
//   Copyright (c) 2021, Improv
//   All rights reserved.
//
//   Redistribution and use in source and binary forms, with or without
//   modification, are permitted provided that the following conditions are
//   met:
//
//   1. Redistributions of source code must retain the above copyright
//      notice, this list of conditions and the following disclaimer.
//
//   2. Redistributions in binary form must reproduce the above copyright
//      notice, this list of conditions and the following disclaimer in the
//      documentation and/or other materials provided with the distribution.
//
//   3. Neither the name of the copyright holder nor the names of its
//      contributors may be used to endorse or promote products derived from
//      this software without specific prior written permission.
//
//   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
//   IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
//   TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
//   PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
//   HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
//   SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
//   TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
//   PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
//   LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
//   NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
//   SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#pragma once

#include <functional>
#include <string>
#include <vector>
#include <cstdint>
#include <cstddef>

namespace improv {

// Wire-protocol version. Every packet begins with the 6 ASCII bytes
// "IMPROV" followed by this version byte.
static const uint8_t IMPROV_SERIAL_VERSION = 1;

// Top-level packet kinds (the byte that follows the version byte).
enum ImprovSerialType : uint8_t {
  TYPE_CURRENT_STATE = 0x01,
  TYPE_ERROR_STATE = 0x02,
  TYPE_RPC = 0x03,
  TYPE_RPC_RESPONSE = 0x04,
};

// RPC command identifiers (first data byte of a TYPE_RPC packet).
enum Command : uint8_t {
  UNKNOWN = 0x00,
  WIFI_SETTINGS = 0x01,
  GET_CURRENT_STATE = 0x02,
  GET_DEVICE_INFO = 0x03,
  GET_WIFI_NETWORKS = 0x04,
  BAD_CHECKSUM = 0xFF,
};

// Device-state values carried in a TYPE_CURRENT_STATE packet.
enum State : uint8_t {
  STATE_STOPPED = 0x00,
  STATE_AWAITING_AUTHORIZATION = 0x01,
  STATE_AUTHORIZED = 0x02,
  STATE_PROVISIONING = 0x03,
  STATE_PROVISIONED = 0x04,
};

// Error codes carried in a TYPE_ERROR_STATE packet.
enum Error : uint8_t {
  ERROR_NONE = 0x00,
  ERROR_INVALID_RPC = 0x01,
  ERROR_UNKNOWN_RPC = 0x02,
  ERROR_UNABLE_TO_CONNECT = 0x03,
  ERROR_NOT_AUTHORIZED = 0x04,
  ERROR_UNKNOWN = 0xFF,
};

// A decoded RPC command: the command id plus its decoded argument list.
// For WIFI_SETTINGS, strings[0] is the SSID and strings[1] is the password.
struct ImprovCommand {
  Command command;
  std::string ssid;
  std::string password;
};

// Decode the data payload of a TYPE_RPC packet into an ImprovCommand.
// `data` is the full packet data field: [command][data_len][args...].
inline ImprovCommand parse_improv_data(const std::vector<uint8_t> &data, bool check_checksum = true);
inline ImprovCommand parse_improv_data(const uint8_t *data, size_t length, bool check_checksum = true);

// Feed one received byte to the incremental parser.
//
// `position` is the running byte index inside the packet (caller owns it and
// must increment after each call — the parser uses it to know which field a
// byte belongs to). `byte` is the byte just read. `buffer` is caller-owned
// scratch that accumulates the packet. `on_command_callback` fires with a
// fully-decoded, checksum-verified RPC command; `on_error_callback` fires on
// a malformed/short packet (e.g. bad checksum).
//
// Returns true while the byte is part of a valid in-progress OR complete
// Improv packet, and false the moment the byte breaks the expected framing.
// A false return means "this byte was NOT an Improv byte" — the caller should
// reset `position` to 0 and route the byte elsewhere (Orchard routes it to
// the line-oriented serial console). This boolean is the coexistence
// primitive that lets Improv and the ASCII console share one Serial stream.
inline bool parse_improv_serial_byte(size_t position, uint8_t byte, const uint8_t *buffer,
                                     std::function<bool(ImprovCommand)> &&on_command_callback,
                                     std::function<void(Error)> &&on_error_callback);

// Build a TYPE_RPC_RESPONSE packet body (data field only, NOT framed) for the
// given command, carrying `datum` as a list of length-prefixed UTF-8 strings.
// When `add_checksum` is true the returned vector also includes the trailing
// 1-byte checksum (sum of all preceding bytes mod 256) — but note the bytes
// returned here are the *data field* contents; framing (header + version +
// type + length + payload-checksum) is added by `build_rpc_response`.
inline std::vector<uint8_t> build_rpc_response(Command command, const std::vector<std::string> &datum,
                                               bool add_checksum = true);

// ----------------------------------------------------------------------------
// Implementation (inline so the header is self-contained).
// ----------------------------------------------------------------------------

inline ImprovCommand parse_improv_data(const std::vector<uint8_t> &data, bool check_checksum) {
  return parse_improv_data(data.data(), data.size(), check_checksum);
}

inline ImprovCommand parse_improv_data(const uint8_t *data, size_t length, bool check_checksum) {
  ImprovCommand improv_command;
  Command command = (Command) data[0];
  uint8_t data_length = data[1];

  if (data_length != length - 2 - (check_checksum ? 1 : 0)) {
    improv_command.command = UNKNOWN;
    return improv_command;
  }

  if (check_checksum) {
    uint8_t checksum = data[length - 1];

    uint32_t calculated_checksum = 0;
    for (uint8_t i = 0; i < length - 1; i++) {
      calculated_checksum += data[i];
    }

    if ((uint8_t) calculated_checksum != checksum) {
      improv_command.command = BAD_CHECKSUM;
      return improv_command;
    }
  }

  if (command == WIFI_SETTINGS) {
    uint8_t ssid_length = data[2];
    uint8_t ssid_start = 3;
    size_t ssid_end = ssid_start + ssid_length;

    uint8_t pass_length = data[ssid_end];
    size_t pass_start = ssid_end + 1;
    size_t pass_end = pass_start + pass_length;

    std::string ssid(data + ssid_start, data + ssid_end);
    std::string password(data + pass_start, data + pass_end);
    // NOTE: the upstream reference uses a C++20 designated initializer here
    // (`return {.command = ..., .ssid = ..., .password = ...};`). Rewritten as
    // explicit field assignment so this compiles cleanly under -std=gnu++17
    // (the project's standard) on all build envs. Semantics are identical.
    improv_command.command = command;
    improv_command.ssid = ssid;
    improv_command.password = password;
    return improv_command;
  }

  improv_command.command = command;
  return improv_command;
}

inline bool parse_improv_serial_byte(size_t position, uint8_t byte, const uint8_t *buffer,
                                     std::function<bool(ImprovCommand)> &&on_command_callback,
                                     std::function<void(Error)> &&on_error_callback) {
  // Header: bytes 0..5 must spell "IMPROV", byte 6 is the version.
  if (position == 0)
    return byte == 'I';
  if (position == 1)
    return byte == 'M';
  if (position == 2)
    return byte == 'P';
  if (position == 3)
    return byte == 'R';
  if (position == 4)
    return byte == 'O';
  if (position == 5)
    return byte == 'V';

  if (position == 6)
    return byte == IMPROV_SERIAL_VERSION;

  // byte 7 = packet type, byte 8 = data length.
  if (position <= 8)
    return true;

  uint8_t type = buffer[7];
  uint8_t data_len = buffer[8];

  // Data bytes [9 .. 9+data_len-1] then one checksum byte.
  if (position <= 8 + data_len)
    return true;

  if (position == 8 + data_len + 1) {
    uint8_t checksum = 0x00;
    for (size_t i = 0; i < position; i++)
      checksum += buffer[i];

    if (checksum != byte) {
      on_error_callback(ERROR_INVALID_RPC);
      return false;
    }

    if (type == TYPE_RPC) {
      auto command = parse_improv_data(&buffer[9], data_len, false);
      on_command_callback(command);
    }

    return false;
  }

  return false;
}

inline std::vector<uint8_t> build_rpc_response(Command command, const std::vector<std::string> &datum,
                                               bool add_checksum) {
  std::vector<uint8_t> out;
  uint32_t length = 0;
  out.push_back(command);
  out.push_back(0);  // placeholder for data length
  for (const auto &str : datum) {
    uint8_t len = str.length();
    length += len + 1;
    out.push_back(len);
    out.insert(out.end(), str.begin(), str.end());
  }
  out[1] = length;

  if (add_checksum) {
    uint32_t calculated_checksum = 0;

    for (uint8_t byte : out) {
      calculated_checksum += byte;
    }
    out.push_back(calculated_checksum);
  }
  return out;
}

}  // namespace improv
