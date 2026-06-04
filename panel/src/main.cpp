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
}

void loop() {
  if (!s_panel || !all_frames || g_frames == 0) { delay(1000); return; }

  const uint32_t loop_start = millis();
  uint32_t work_ms = 0;
  uint32_t swap_at = g_vsync + kVsyncPerFrame;   // next swap on the refresh grid
  for (int i = 0; i < g_frames; i++) {
    const uint32_t t0 = millis();
    g_target = s_fb[s_back];
    if (jpeg.openRAM(all_frames + frame_off[i], frame_len[i], jpeg_draw)) {
      jpeg.decode(0, 0, 0);
      jpeg.close();
    }
    work_ms += millis() - t0;
    // Hold each frame for exactly kVsyncPerFrame refreshes, then swap on the
    // vsync boundary. Aligning swaps to the refresh grid removes the 2-3
    // refresh "beat" that made delay()-paced double-buffering judder.
    // Safety cap: if the vsync counter isn't advancing (callback not firing
    // on some IDF builds), bail after ~150ms so the loop never stalls to a
    // black screen — it just falls back to running as fast as it can.
    for (uint32_t ws = millis(); (int32_t)(swap_at - g_vsync) > 0; ) {
      if (millis() - ws > 150) break;
      delay(1);
    }
    esp_lcd_panel_draw_bitmap(s_panel, 0, 0, kHRes, kVRes, s_fb[s_back]);
    s_back ^= 1;
    swap_at += kVsyncPerFrame;
  }
  const uint32_t total = millis() - loop_start;
  Serial.printf("[video] loop %d frames in %ums (work %ums/frame, %.1f fps)\n",
                g_frames, (unsigned)total, (unsigned)(work_ms / g_frames),
                1000.0f * g_frames / total);
}
