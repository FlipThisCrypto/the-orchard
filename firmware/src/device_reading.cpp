// SPDX-License-Identifier: Apache-2.0
#include "device_reading.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <time.h>

#include "identity.h"

namespace orchard {

namespace {

// Sorted metric key names (must match Python sort_keys=True order).
// Only keys with values are emitted.
struct MetricEntry {
  const char* key;
  // 0 = int, 1 = bool
  uint8_t kind;
  int32_t i;
  bool b;
  bool present;
};

char block_anchor_[17] = "0000000000000000";

int64_t parse_gps_utc_ms(const char* iso) {
  // Expect "YYYY-MM-DDTHH:MM:SSZ"
  if (iso == nullptr || strlen(iso) < 20) return -1;
  struct tm t {};
  if (sscanf(iso, "%d-%d-%dT%d:%d:%d",
             &t.tm_year, &t.tm_mon, &t.tm_mday,
             &t.tm_hour, &t.tm_min, &t.tm_sec) != 6) {
    return -1;
  }
  t.tm_year -= 1900;
  t.tm_mon  -= 1;
  // timegm is not on all toolchains; compute UTC manually via mktime after
  // setting TZ is messy on ESP. Use a simple days-from-epoch calculation.
  // Formula: Unix time for UTC calendar components.
  int y = t.tm_year + 1900;
  int m = t.tm_mon + 1;
  int d = t.tm_mday;
  // Sakamoto / civil_from_days inverse (Howard Hinnant)
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = static_cast<unsigned>(y - era * 400);
  const unsigned doy =
      (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  const int64_t days = static_cast<int64_t>(era) * 146097 +
                       static_cast<int64_t>(doe) - 719468;
  const int64_t secs = days * 86400LL + t.tm_hour * 3600LL +
                       t.tm_min * 60LL + t.tm_sec;
  return secs * 1000LL;
}

int64_t resolve_ts_ms(JsonObject sensors) {
  if (!sensors.isNull() && sensors["gps"].is<JsonObject>()) {
    JsonObject gps = sensors["gps"].as<JsonObject>();
    if (gps["utc"].is<const char*>()) {
      int64_t ms = parse_gps_utc_ms(gps["utc"].as<const char*>());
      if (ms > 0) return ms;
    }
  }
  // System clock if SNTP/RTC looks set (year >= 2024).
  time_t now = time(nullptr);
  if (now > 1704067200L) {  // 2024-01-01 UTC
    return static_cast<int64_t>(now) * 1000LL;
  }
  // Last resort: monotonic millis (not wall clock — verifiers may flag it).
  return static_cast<int64_t>(millis());
}

void collect_metrics(JsonObject sensors, MetricEntry* out, size_t* count) {
  *count = 0;
  auto add_int = [&](const char* key, int32_t v) {
    out[*count] = MetricEntry{key, 0, v, false, true};
    (*count)++;
  };
  auto add_bool = [&](const char* key, bool v) {
    out[*count] = MetricEntry{key, 1, 0, v, true};
    (*count)++;
  };

  // Insertion order does not matter — we sort before emit.
  if (!sensors.isNull() && sensors["bme280"].is<JsonObject>()) {
    JsonObject bme = sensors["bme280"].as<JsonObject>();
    if (bme["temperature_c"].is<float>() || bme["temperature_c"].is<double>() ||
        bme["temperature_c"].is<int>()) {
      float t = bme["temperature_c"].as<float>();
      if (!isnan(t)) add_int("temperature_mc", static_cast<int32_t>(lroundf(t * 1000.0f)));
    }
    if (bme["humidity_pct"].is<float>() || bme["humidity_pct"].is<double>() ||
        bme["humidity_pct"].is<int>()) {
      float h = bme["humidity_pct"].as<float>();
      if (!isnan(h)) add_int("humidity_milli_pct", static_cast<int32_t>(lroundf(h * 1000.0f)));
    }
    if (bme["pressure_hpa"].is<float>() || bme["pressure_hpa"].is<double>() ||
        bme["pressure_hpa"].is<int>()) {
      float p = bme["pressure_hpa"].as<float>();
      if (!isnan(p)) {
        // hPa → Pa (×100); if already Pa (>2000), keep.
        int32_t pa = (p < 2000.0f)
                         ? static_cast<int32_t>(lroundf(p * 100.0f))
                         : static_cast<int32_t>(lroundf(p));
        add_int("pressure_pa", pa);
      }
    }
  }

  if (!sensors.isNull() && sensors["ds18b20"].is<JsonObject>()) {
    JsonObject ds = sensors["ds18b20"].as<JsonObject>();
    bool have_temp = false;
    for (size_t i = 0; i < *count; ++i) {
      if (strcmp(out[i].key, "temperature_mc") == 0) have_temp = true;
    }
    if (!have_temp &&
        (ds["temperature_c"].is<float>() || ds["temperature_c"].is<double>() ||
         ds["temperature_c"].is<int>())) {
      float t = ds["temperature_c"].as<float>();
      if (!isnan(t)) add_int("temperature_mc", static_cast<int32_t>(lroundf(t * 1000.0f)));
    }
  }

  if (!sensors.isNull() && sensors["mq135"].is<JsonObject>()) {
    JsonObject mq = sensors["mq135"].as<JsonObject>();
    float raw = NAN;
    if (mq["adc_raw"].is<float>() || mq["adc_raw"].is<double>() ||
        mq["adc_raw"].is<int>()) {
      raw = mq["adc_raw"].as<float>();
    } else if (mq["adc"].is<float>() || mq["adc"].is<int>()) {
      raw = mq["adc"].as<float>();
    }
    if (!isnan(raw)) {
      int32_t adc = static_cast<int32_t>(lroundf(raw));
      add_int("gas_adc_raw", adc);
      add_int("gas_mv", static_cast<int32_t>(lroundf(adc * 3300.0f / 4095.0f)));
    }
  }

  if (!sensors.isNull() && sensors["gps"].is<JsonObject>()) {
    JsonObject gps = sensors["gps"].as<JsonObject>();
    if (gps["fix"].is<bool>()) {
      add_bool("gps_fix", gps["fix"].as<bool>());
    }
    int sats = -1;
    if (gps["sats"].is<int>()) sats = gps["sats"].as<int>();
    else if (gps["satellites"].is<int>()) sats = gps["satellites"].as<int>();
    if (sats >= 0) add_int("gps_sats", sats);
  }

  // Sort by key (strcmp) so canonical matches Python sort_keys=True.
  for (size_t i = 1; i < *count; ++i) {
    MetricEntry tmp = out[i];
    size_t j = i;
    while (j > 0 && strcmp(out[j - 1].key, tmp.key) > 0) {
      out[j] = out[j - 1];
      --j;
    }
    out[j] = tmp;
  }
}

// Build canonical JSON for the unsigned reading body into `out`.
// Returns written length (not including NUL), or -1 on overflow.
int build_canonical(char* out, size_t cap,
                    const char* node_id,
                    int64_t ts_ms,
                    const char* block_anchor,
                    const MetricEntry* metrics,
                    size_t n_metrics) {
  // Keys sorted: block_anchor, metrics, node_id, ts_ms
  int n = snprintf(out, cap,
                   "{\"block_anchor\":\"%s\",\"metrics\":{",
                   block_anchor);
  if (n < 0 || static_cast<size_t>(n) >= cap) return -1;
  size_t pos = static_cast<size_t>(n);

  for (size_t i = 0; i < n_metrics; ++i) {
    char piece[96];
    int pn;
    if (metrics[i].kind == 1) {
      pn = snprintf(piece, sizeof(piece), "%s\"%s\":%s",
                    (i ? "," : ""),
                    metrics[i].key,
                    metrics[i].b ? "true" : "false");
    } else {
      pn = snprintf(piece, sizeof(piece), "%s\"%s\":%ld",
                    (i ? "," : ""),
                    metrics[i].key,
                    static_cast<long>(metrics[i].i));
    }
    if (pn < 0 || pos + static_cast<size_t>(pn) + 1 >= cap) return -1;
    memcpy(out + pos, piece, static_cast<size_t>(pn));
    pos += static_cast<size_t>(pn);
  }

  n = snprintf(out + pos, cap - pos,
               "},\"node_id\":\"%s\",\"ts_ms\":%lld}",
               node_id,
               static_cast<long long>(ts_ms));
  if (n < 0 || pos + static_cast<size_t>(n) >= cap) return -1;
  return static_cast<int>(pos + static_cast<size_t>(n));
}

}  // namespace

void set_block_anchor(const char* hex16) {
  if (hex16 == nullptr || strlen(hex16) < 16) {
    memcpy(block_anchor_, "0000000000000000", 17);
    return;
  }
  for (int i = 0; i < 16; ++i) {
    char c = hex16[i];
    if (c >= 'A' && c <= 'F') c = static_cast<char>(c - 'A' + 'a');
    block_anchor_[i] = c;
  }
  block_anchor_[16] = '\0';
}

bool attach_device_reading(JsonDocument& payload) {
  JsonObject sensors = payload["sensors"].as<JsonObject>();
  MetricEntry metrics[16];
  size_t n_metrics = 0;
  collect_metrics(sensors, metrics, &n_metrics);
  if (n_metrics == 0) {
    Serial.println("[device_reading] no metrics; skip sign");
    return false;
  }

  const String& node = identity::node_id_hex();
  const int64_t ts_ms = resolve_ts_ms(sensors);
  // Surface wall-clock ts on the transport payload too (oracle tree_ts_ms).
  payload["ts_ms"] = ts_ms;

  char canonical[768];
  const int clen = build_canonical(canonical, sizeof(canonical),
                                   node.c_str(), ts_ms, block_anchor_,
                                   metrics, n_metrics);
  if (clen < 0) {
    Serial.println("[device_reading] canonical overflow");
    return false;
  }

  uint8_t sig[identity::kP256SigLen];
  identity::p256_sign(reinterpret_cast<const uint8_t*>(canonical),
                      static_cast<size_t>(clen), sig);
  // All-zero sig means sign failed.
  bool all_zero = true;
  for (size_t i = 0; i < sizeof(sig); ++i) {
    if (sig[i] != 0) { all_zero = false; break; }
  }
  if (all_zero) {
    Serial.println("[device_reading] p256_sign failed");
    return false;
  }
  const String sig_hex = identity::to_hex_lower(sig, sizeof(sig));

  JsonObject dr = payload["device_reading"].to<JsonObject>();
  dr["node_id"] = node;
  dr["ts_ms"] = ts_ms;
  dr["block_anchor"] = block_anchor_;
  JsonObject mobj = dr["metrics"].to<JsonObject>();
  for (size_t i = 0; i < n_metrics; ++i) {
    if (metrics[i].kind == 1) {
      mobj[metrics[i].key] = metrics[i].b;
    } else {
      mobj[metrics[i].key] = metrics[i].i;
    }
  }
  dr["sig"] = sig_hex;

  Serial.printf("[device_reading] signed %d metric(s) ts_ms=%lld canon=%dB\n",
                static_cast<int>(n_metrics),
                static_cast<long long>(ts_ms),
                clen);
  return true;
}

}  // namespace orchard
