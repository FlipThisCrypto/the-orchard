// SPDX-License-Identifier: Apache-2.0
#include "ota.h"

#include <Arduino.h>
#include <Update.h>
#include <WebServer.h>
#include <WiFi.h>

#include "config.h"
#include "identity.h"
#include "version.h"

namespace orchard::net {

namespace {

WebServer server_(ORCHARD_DEVICE_HTTP_PORT);
bool started_ = false;
// OTA arm window (C1 hardening). /ota only accepts an upload while armed;
// arming requires the local OTA_ARM serial command.
uint32_t armed_until_ms_ = 0;
bool upload_rejected_ = false;

bool ota_is_armed_() {
  return armed_until_ms_ != 0 && (int32_t)(armed_until_ms_ - millis()) > 0;
}

void handle_health_() {
  String body;
  body.reserve(192);
  body += "{\"node_id\":\"";
  body += identity::node_id_hex();
  body += "\",\"fw\":\"";
  body += orchard::kFirmwareVersion;
  body += "\",\"uptime_ms\":";
  body += millis();
  body += "}";
  server_.send(200, "application/json", body);
}

void handle_ota_upload_() {
  HTTPUpload& upload = server_.upload();
  if (upload.status == UPLOAD_FILE_START) {
    if (!ota_is_armed_()) {
      // Reject: refuse to even start an Update unless armed via serial.
      upload_rejected_ = true;
      Serial.println("[ota] REJECTED upload — not armed (run OTA_ARM on the "
                     "serial console first)");
      return;
    }
    upload_rejected_ = false;
    Serial.printf("[ota] starting update, name=%s\n", upload.filename.c_str());
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
      Update.printError(Serial);
    }
  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if (upload_rejected_) return;
    if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
      Update.printError(Serial);
    }
  } else if (upload.status == UPLOAD_FILE_END) {
    if (upload_rejected_) return;
    if (Update.end(/*evenIfRemaining=*/true)) {
      Serial.printf("[ota] update OK, %u bytes\n", upload.totalSize);
    } else {
      Update.printError(Serial);
    }
  }
}

void handle_ota_done_() {
  if (upload_rejected_) {
    upload_rejected_ = false;
    server_.send(403, "text/plain",
                 "OTA not armed — run OTA_ARM on the serial console first");
    return;
  }
  if (Update.hasError()) {
    server_.send(500, "text/plain", "OTA failed");
  } else {
    armed_until_ms_ = 0;  // consume the arm window on a successful flash
    server_.send(200, "text/plain", "OK; rebooting");
    delay(500);
    ESP.restart();
  }
}

}  // namespace

void ota_arm(uint32_t window_ms) {
  armed_until_ms_ = millis() + window_ms;
  Serial.printf("[ota] armed for %lu ms — POST /ota now\n",
                (unsigned long)window_ms);
}

void ota_begin() {
  if (started_) return;
  server_.on("/health", HTTP_GET, handle_health_);
  server_.on("/ota", HTTP_POST, handle_ota_done_, handle_ota_upload_);
  server_.begin();
  started_ = true;
  Serial.printf("[ota] http server listening on :%d\n",
                ORCHARD_DEVICE_HTTP_PORT);
}

void ota_loop() {
  if (!started_) {
    if (WiFi.status() == WL_CONNECTED) ota_begin();
    return;
  }
  server_.handleClient();
}

}  // namespace orchard::net
