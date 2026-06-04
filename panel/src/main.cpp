// SPDX-License-Identifier: Apache-2.0
//
// The Orchard — Panel firmware (Phase 9.2)
// Board: Waveshare ESP32-S3-Touch-LCD-4.3 / 4.3B (800x480 RGB565 parallel)
//
// PHASE 2.5 — DOUBLE-BUFFERED video (tear-free).
//   Driven via native esp_lcd (arduino-esp32 3.x / IDF 5.x) with TWO
//   framebuffers (num_fbs = 2). We decode each JPEG frame straight into the
//   back buffer's centered window, then esp_lcd_panel_draw_bitmap() hands it
//   to the LCD EDMA, which only swaps to it AFTER the current frame finishes
//   scanning out. Result: no tearing, and no separate blit copy.
//
//   Boot shows ~1.5s of color bars first (confirms the panel + new driver
//   light up) before the video loop begins.
//
// Frames: ffmpeg -> 528x296 JPEGs @ 12fps in LittleFS /v/ (via uploadfs).

#include <Arduino.h>
#include <Wire.h>
#include <LittleFS.h>
#include <JPEGDEC.h>
#include "esp_lcd_panel_rgb.h"
#include "esp_lcd_panel_ops.h"
#include "font8x16.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef ORCHARD_WIFI_SSID
#define ORCHARD_WIFI_SSID  ""
#define ORCHARD_WIFI_PASS  ""
#define ORCHARD_ORACLE_URL "http://192.168.0.223:8000"
#endif

// ---- panel geometry ---------------------------------------------------
static constexpr int kHRes = 800;
static constexpr int kVRes = 480;

// ---- video window -----------------------------------------------------
static constexpr int kVidW = 528;
static constexpr int kVidH = 296;
static constexpr int kVidX = 8;                   // hard left (keeps 5px for border)
static constexpr int kVidY = (kVRes - kVidH) / 2; // 92
static constexpr int kVsyncPerFrame = 2;  // hold each frame N panel refreshes
static constexpr int kMaxFrames = 600;

// ---- I2C / CH422G backlight ------------------------------------------
static constexpr int kI2cSda = 8;
static constexpr int kI2cScl = 9;
static void ch422g_displays_on() {
  Wire.beginTransmission(0x24); Wire.write(0x01); Wire.endTransmission();
  Wire.beginTransmission(0x38); Wire.write(0xFF); Wire.endTransmission();
}

// ---- esp_lcd panel + framebuffers ------------------------------------
static esp_lcd_panel_handle_t s_panel = nullptr;
static uint16_t *s_fb[2] = {nullptr, nullptr};
static int s_back = 1;                 // index of the buffer not on screen
static uint16_t *g_target = nullptr;   // where the JPEG decoder writes

// VSYNC counter, bumped in the panel's vsync ISR. We pace frame swaps onto
// an exact refresh-grid (every kVsyncPerFrame vsyncs) so the cadence is even.
static volatile uint32_t g_vsync = 0;
static bool IRAM_ATTR on_vsync(esp_lcd_panel_handle_t panel,
                               const esp_lcd_rgb_panel_event_data_t *edata,
                               void *user_ctx) {
  (void)panel; (void)edata; (void)user_ctx;
  g_vsync++;
  return false;
}

static bool panel_init() {
  esp_lcd_rgb_panel_config_t cfg = {};   // zero-init; set fields individually
  cfg.clk_src = LCD_CLK_SRC_DEFAULT;
  cfg.timings.pclk_hz = 16 * 1000 * 1000;
  cfg.timings.h_res = kHRes;
  cfg.timings.v_res = kVRes;
  cfg.timings.hsync_pulse_width = 48;
  cfg.timings.hsync_back_porch   = 88;
  cfg.timings.hsync_front_porch  = 40;
  cfg.timings.vsync_pulse_width = 3;
  cfg.timings.vsync_back_porch   = 32;
  cfg.timings.vsync_front_porch  = 13;
  cfg.timings.flags.pclk_active_neg = 1;
  cfg.data_width = 16;
  cfg.num_fbs = 2;                       // <-- double buffer (tear-free)
  // DMA scans the framebuffer through a small internal-SRAM "bounce"
  // buffer instead of straight from PSRAM. This decouples scan-out from
  // PSRAM bandwidth, killing the horizontal "pulling right" glitches that
  // appear when the JPEG decoder pounds PSRAM mid-frame.
  cfg.bounce_buffer_size_px = kHRes * 10;
  cfg.hsync_gpio_num = 46;
  cfg.vsync_gpio_num = 3;
  cfg.de_gpio_num    = 5;
  cfg.pclk_gpio_num  = 7;
  cfg.disp_gpio_num  = -1;               // backlight is via CH422G, not a pin
  // data pins, RGB565 order: B0-B4, G0-G5, R0-R4
  const int dpins[16] = {14, 38, 18, 17, 10,  39, 0, 45, 48, 47, 21,  1, 2, 42, 41, 40};
  for (int i = 0; i < 16; i++) cfg.data_gpio_nums[i] = dpins[i];
  cfg.flags.fb_in_psram = 1;

  esp_err_t e = esp_lcd_new_rgb_panel(&cfg, &s_panel);
  if (e != ESP_OK) { Serial.printf("[lcd] new_rgb_panel: %d\n", e); return false; }
  esp_lcd_panel_reset(s_panel);
  esp_lcd_panel_init(s_panel);
  e = esp_lcd_rgb_panel_get_frame_buffer(s_panel, 2, (void **)&s_fb[0], (void **)&s_fb[1]);
  if (e != ESP_OK || !s_fb[0] || !s_fb[1]) {
    Serial.printf("[lcd] get_frame_buffer: %d (%p,%p)\n", e, s_fb[0], s_fb[1]);
    return false;
  }
  esp_lcd_rgb_panel_event_callbacks_t cbs = {};
  cbs.on_vsync = on_vsync;
  esp_lcd_rgb_panel_register_event_callbacks(s_panel, &cbs, nullptr);
  Serial.printf("[lcd] ok, fbs=%p,%p\n", s_fb[0], s_fb[1]);
  return true;
}

static inline void fb_fill(uint16_t *fb, uint16_t c) {
  for (size_t i = 0; i < (size_t)kHRes * kVRes; i++) fb[i] = c;
}
static void fb_rect(uint16_t *fb, int x, int y, int w, int h, uint16_t c) {
  for (int j = 0; j < h; j++) {
    if (y + j < 0 || y + j >= kVRes) continue;
    uint16_t *row = fb + (size_t)(y + j) * kHRes + x;
    for (int i = 0; i < w; i++) row[i] = c;
  }
}
// Border frame around the video window, drawn into a given fb.
static void fb_decor(uint16_t *fb) {
  const uint16_t accent = 0x9F6C;  // ~Orchard green in RGB565
  for (int t = 1; t <= 3; t++) {
    fb_rect(fb, kVidX - t, kVidY - t, kVidW + 2 * t, 1, accent);
    fb_rect(fb, kVidX - t, kVidY + kVidH + t - 1, kVidW + 2 * t, 1, accent);
    fb_rect(fb, kVidX - t, kVidY - t, 1, kVidH + 2 * t, accent);
    fb_rect(fb, kVidX + kVidW + t - 1, kVidY - t, 1, kVidH + 2 * t, accent);
  }
}

// ---- MJPEG ------------------------------------------------------------
static JPEGDEC jpeg;
static uint8_t *all_frames = nullptr;
static size_t   frame_off[kMaxFrames];
static size_t   frame_len[kMaxFrames];
static int g_frames = 0;

// Decode MCU blocks straight into the back framebuffer's centered window.
static int jpeg_draw(JPEGDRAW *p) {
  for (int row = 0; row < p->iHeight; row++) {
    int y = p->y + row;
    if (y < 0 || y >= kVidH) continue;
    int x = p->x, w = p->iWidth, srcoff = 0;
    if (x < 0) { srcoff = -x; w += x; x = 0; }
    if (x + w > kVidW) w = kVidW - x;
    if (w <= 0) continue;
    memcpy(g_target + (size_t)(kVidY + y) * kHRes + (kVidX + x),
           p->pPixels + (size_t)row * p->iWidth + srcoff, (size_t)w * 2);
  }
  return 1;
}

static void preload_frames() {
  g_frames = 0;
  size_t total = 0;
  for (int i = 0; i < kMaxFrames; i++) {
    char path[32];
    snprintf(path, sizeof(path), "/v/%03d.jpg", i);
    File f = LittleFS.open(path, "r");
    if (!f) break;
    frame_len[i] = f.size(); total += frame_len[i]; f.close(); g_frames++;
  }
  if (!g_frames) { Serial.println("[video] no frames"); return; }
  all_frames = (uint8_t *)ps_malloc(total);
  if (!all_frames) { Serial.printf("[video] ps_malloc(%u) FAIL\n", (unsigned)total); g_frames = 0; return; }
  size_t off = 0;
  for (int i = 0; i < g_frames; i++) {
    char path[32];
    snprintf(path, sizeof(path), "/v/%03d.jpg", i);
    File f = LittleFS.open(path, "r");
    if (!f) continue;
    f.read(all_frames + off, frame_len[i]); f.close();
    frame_off[i] = off; off += frame_len[i];
  }
  Serial.printf("[video] preloaded %d frames (%u bytes)\n", g_frames, (unsigned)total);
}

// ---- text (8x16 bitmap font, integer-scaled) --------------------------
static void draw_char(uint16_t *fb, int x, int y, char c, int scale, uint16_t color) {
  const uint8_t *g = font_glyph(c);
  for (int row = 0; row < 16; row++) {
    const uint8_t bits = g[row];
    if (!bits) continue;
    for (int col = 0; col < 8; col++) {
      if (!(bits & (0x80 >> col))) continue;
      for (int dy = 0; dy < scale; dy++) {
        int py = y + row * scale + dy;
        if (py < 0 || py >= kVRes) continue;
        uint16_t *r = fb + (size_t)py * kHRes;
        for (int dx = 0; dx < scale; dx++) {
          int px = x + col * scale + dx;
          if (px >= 0 && px < kHRes) r[px] = color;
        }
      }
    }
  }
}
static void draw_text(uint16_t *fb, int x, int y, const char *s, int scale, uint16_t color) {
  for (; *s; s++) { draw_char(fb, x, y, *s, scale, color); x += 8 * scale; }
}

// ---- GT911 capacitive touch (I2C, shared bus on GPIO 8/9) -------------
static uint8_t s_gt_addr = 0x5D;
static void gt911_init() {
  for (uint8_t a : {0x5D, 0x14}) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { s_gt_addr = a; Serial.printf("[touch] GT911 at 0x%02X\n", a); return; }
  }
  Serial.println("[touch] GT911 not found (tried 0x5D/0x14)");
}
// True if a finger is down; fills x,y with the first point. The GT911's
// 0x814E status byte: bit7=data ready, low nibble = number of points. We
// must clear bit7 after reading or it stops reporting.
static bool gt911_touched(int *x, int *y) {
  Wire.beginTransmission(s_gt_addr);
  Wire.write(0x81); Wire.write(0x4E);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(s_gt_addr, (uint8_t)1) != 1) return false;
  uint8_t st = Wire.read();
  bool down = false;
  if (st & 0x80) {
    if ((st & 0x0F) > 0) {
      Wire.beginTransmission(s_gt_addr);
      Wire.write(0x81); Wire.write(0x50);          // point 1, X-low
      Wire.endTransmission(false);
      if (Wire.requestFrom(s_gt_addr, (uint8_t)4) == 4) {
        int xl = Wire.read(), xh = Wire.read(), yl = Wire.read(), yh = Wire.read();
        *x = xl | (xh << 8); *y = yl | (yh << 8);
        down = true;
      }
    }
    Wire.beginTransmission(s_gt_addr);
    Wire.write(0x81); Wire.write(0x4E); Wire.write(0x00);
    Wire.endTransmission();
  }
  return down;
}

// ---- views (tap toggles) ----------------------------------------------
enum View { V_VIDEO, V_STATS };
static View g_view = V_VIDEO;
static bool g_dirty = false;      // static content of the current view needs redraw
static int  g_vframe = 0;         // current video frame index
static bool g_was_down = false;
static uint32_t g_swap_at = 0;

// ---- live network stats (fetched from the oracle over WiFi) -----------
struct NetStats { bool valid = false; long trees = 0, readings24 = 0, attest = 0, season = 0; };
static NetStats g_stats;
static uint32_t g_last_fetch = 0;

static void wifi_begin() {
  if (strlen(ORCHARD_WIFI_SSID) == 0) {
    Serial.println("[wifi] no SSID — set it in src/secrets.h");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(ORCHARD_WIFI_SSID, ORCHARD_WIFI_PASS);
  Serial.printf("[wifi] connecting to '%s'\n", ORCHARD_WIFI_SSID);
}

static void fetch_stats() {
  g_last_fetch = millis();
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = String(ORCHARD_ORACLE_URL) + "/network/stats";
  if (!http.begin(url)) return;
  http.setConnectTimeout(2000);
  http.setTimeout(3000);
  const int code = http.GET();
  if (code == 200) {
    JsonDocument doc;
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok) {
      g_stats.trees      = doc["trees_active_24h"]   | 0;
      g_stats.readings24 = doc["readings_last_24h"]  | 0;
      g_stats.attest     = doc["attestations_total"] | 0;
      g_stats.season     = doc["current_season"]     | 0;
      g_stats.valid = true;
      Serial.printf("[stats] trees=%ld readings24=%ld attest=%ld season=%ld\n",
                    g_stats.trees, g_stats.readings24, g_stats.attest, g_stats.season);
    }
  } else {
    Serial.printf("[stats] HTTP GET -> %d\n", code);
  }
  http.end();
}

static void stat_row(uint16_t *fb, int y, const char *label, long val,
                     uint16_t lc, uint16_t vc, uint16_t mc) {
  draw_text(fb, 40, y, label, 3, lc);
  if (g_stats.valid) { char b[16]; snprintf(b, sizeof(b), "%ld", val); draw_text(fb, 540, y, b, 3, vc); }
  else               { draw_text(fb, 540, y, "--", 3, mc); }
}

static void render_stats(uint16_t *fb) {
  fb_fill(fb, 0x0000);
  const uint16_t green = 0x9F6C, white = 0xFFFF, amber = 0xFD20, grey = 0x8410;
  draw_text(fb, 40, 26, "THE ORCHARD",   4, green);
  draw_text(fb, 42, 92, "NETWORK STATS", 2, white);
  int y = 150;
  stat_row(fb, y, "TREES ONLINE", g_stats.trees,      white, amber, grey); y += 58;
  stat_row(fb, y, "READINGS 24H", g_stats.readings24, white, amber, grey); y += 58;
  stat_row(fb, y, "ATTESTATIONS", g_stats.attest,     white, amber, grey); y += 58;
  stat_row(fb, y, "SEASON",       g_stats.season,     white, amber, grey);
  const char *status = (WiFi.status() == WL_CONNECTED)
      ? (g_stats.valid ? "live  -  tap to return to video"
                       : "connected, fetching...  -  tap for video")
      : "wifi connecting...  (set password in secrets.h)";
  draw_text(fb, 40, 448, status, 1, grey);
}

// Wait for the scheduled vsync, then hand the back buffer to the LCD.
static void wait_and_swap() {
  for (uint32_t ws = millis(); (int32_t)(g_swap_at - g_vsync) > 0; ) {
    if (millis() - ws > 150) break;   // never stall to black if vsync is silent
    delay(1);
  }
  esp_lcd_panel_draw_bitmap(s_panel, 0, 0, kHRes, kVRes, s_fb[s_back]);
  s_back ^= 1;
  g_swap_at += kVsyncPerFrame;
}

static void poll_touch() {
  int x = 0, y = 0;
  bool down = gt911_touched(&x, &y);
  if (down && !g_was_down) {           // rising edge = a tap
    g_view = (g_view == V_VIDEO) ? V_STATS : V_VIDEO;
    g_dirty = true;
    Serial.printf("[touch] tap @%d,%d -> %s\n", x, y, g_view == V_VIDEO ? "VIDEO" : "STATS");
  }
  g_was_down = down;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== The Orchard Panel (Phase 2.5 - double-buffered) ===");
  Serial.printf("fw=%s  flash=%uMB  psram=%uMB\n", ORCHARD_PANEL_VERSION,
                (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)),
                (unsigned)(ESP.getPsramSize() / (1024 * 1024)));

  Wire.begin(kI2cSda, kI2cScl);
  ch422g_displays_on();

  if (!panel_init()) { Serial.println("[lcd] PANEL INIT FAILED"); }

  // Boot test pattern: color bars on fb0 — confirms the panel + new driver.
  if (s_fb[0]) {
    fb_fill(s_fb[0], 0x0000);
    const uint16_t bars[] = {0xF800, 0x07E0, 0x001F, 0xFFFF, 0xFFE0, 0x07FF, 0xF81F};
    for (int b = 0; b < 7; b++) fb_rect(s_fb[0], b * (kHRes / 7), 0, kHRes / 7, 120, bars[b]);
    esp_lcd_panel_draw_bitmap(s_panel, 0, 0, kHRes, kVRes, s_fb[0]);
    delay(1500);
  }

  if (!LittleFS.begin(false, "/littlefs", 10, "storage"))
    Serial.println("[fs] LittleFS mount FAILED");
  preload_frames();

  // Prep both framebuffers: black + border, so the decor persists across
  // swaps (the per-frame decode only touches the inner window).
  for (int i = 0; i < 2; i++) { if (s_fb[i]) { fb_fill(s_fb[i], 0x0000); fb_decor(s_fb[i]); } }
  s_back = 1;
  gt911_init();
  wifi_begin();
  g_swap_at = g_vsync + kVsyncPerFrame;
}

void loop() {
  if (!s_panel) { delay(1000); return; }

  // Log + prime the stats once WiFi comes up, so the first tap to the stats
  // view shows live numbers immediately (one-time ~0.5s fetch).
  static bool wifi_logged = false;
  if (!wifi_logged && WiFi.status() == WL_CONNECTED) {
    wifi_logged = true;
    Serial.print("[wifi] connected, ip="); Serial.println(WiFi.localIP());
    fetch_stats();
  }

  poll_touch();

  if (g_view == V_VIDEO) {
    if (!all_frames || g_frames == 0) { delay(100); return; }
    if (g_dirty) {                 // returning from STATS: restore the decor
      for (int i = 0; i < 2; i++) { fb_fill(s_fb[i], 0x0000); fb_decor(s_fb[i]); }
      g_swap_at = g_vsync + kVsyncPerFrame;     // re-anchor the refresh grid
      g_dirty = false;
    }
    g_target = s_fb[s_back];
    if (jpeg.openRAM(all_frames + frame_off[g_vframe], frame_len[g_vframe], jpeg_draw)) {
      jpeg.decode(0, 0, 0);
      jpeg.close();
    }
    g_vframe = (g_vframe + 1) % g_frames;
    wait_and_swap();               // vsync-locked: even cadence, tear-free
  } else {                         // V_STATS — fetch from oracle + render
    if (g_dirty) {
      g_swap_at = g_vsync + kVsyncPerFrame;
      render_stats(s_fb[s_back]); wait_and_swap();   // immediate feedback
      render_stats(s_fb[s_back]); wait_and_swap();
      fetch_stats();                                  // ~0.5s blocking HTTP GET
      render_stats(s_fb[s_back]); wait_and_swap();    // now with live numbers
      render_stats(s_fb[s_back]); wait_and_swap();
      g_dirty = false;
    } else {
      if (millis() - g_last_fetch > 10000) {          // refresh every 10s
        fetch_stats();
        render_stats(s_fb[s_back]); wait_and_swap();
        render_stats(s_fb[s_back]); wait_and_swap();
      }
      delay(20);                   // idle; keep polling touch responsively
    }
  }
}
