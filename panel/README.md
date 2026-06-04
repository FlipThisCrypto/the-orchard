# panel/ — The Orchard status panel (Phase 9.2)

A physical, touch-screen status panel for The Orchard, on the **Waveshare
ESP32-S3-Touch-LCD-4.3 / 4.3B** (800×480 RGB capacitive touch).

It loops a video, and a **tap** flips to a live **Orchard network stats**
screen pulled from your oracle over WiFi. (A third view — a live MQ-2 gas
reading — lands once that sensor is wired.)

> Separate project from `firmware/` (the Tree sensor node): different board,
> different libraries, different job.

## What works

| View | |
|---|---|
| **Video** | Smooth, tear-free 528×296 hologram loop, centered + framed |
| **Stats** (tap) | Live Trees online / Readings 24h / Attestations / Season from the oracle's `/network/stats`, refreshed every 10s |
| **MQ-2** (planned) | Live gas reading — needs the sensor wired (see below) |

## How it's built

- **Display:** native `esp_lcd` RGB driver with **two PSRAM framebuffers**
  (`num_fbs=2`) on the **pioarduino** core (arduino-esp32 3.x / IDF 5.x) — no
  Arduino_GFX. Smooth video needed three stacked fixes, each killing one
  artifact seen on real hardware:
  - **double buffer** → no tearing
  - **vsync-locked cadence** (2 refreshes/frame ≈ 15.5fps) → no judder
  - **bounce buffer** → no PSRAM-bandwidth "pulling right" glitches
- **Video:** `.mp4` transcoded on the host to 528×296 JPEGs @ 15fps (in
  LittleFS `/v/`), preloaded to PSRAM, JPEG-decoded straight into the back
  framebuffer.
- **Text:** a generated 8×16 Consolas bitmap font (`src/font8x16.h`).
- **Touch:** GT911 over I²C (0x5D), polled each frame; a tap toggles views.
- **Backlight / LCD+touch reset:** CH422G I²C expander.

## Build & flash

> ⚠️ The pioarduino core's tooling must run from **native PowerShell with
> `PYTHONIOENCODING=utf-8`**. Under Git-Bash/MSys it errors out, and the new
> esptool's Unicode output can otherwise stall the flasher mid-write.

```powershell
# WiFi creds (gitignored — never committed):
copy panel\src\secrets.h.example panel\src\secrets.h   # then edit SSID/password/oracle URL

$env:PYTHONIOENCODING = "utf-8"
$P = "I:\DeMeter Data\Chia DePIN\panel"
python -m platformio run -d $P -e waveshare_s3_touch_43b -t uploadfs --upload-port COM<n>  # video frames
python -m platformio run -d $P -e waveshare_s3_touch_43b -t upload   --upload-port COM<n>  # firmware
python -m platformio device monitor --port COM<n>
```

The board flashes/serials over its onboard **CH343 UART bridge** (shows up as
a COM port), not native USB — hence `ARDUINO_USB_CDC_ON_BOOT=0`.

## Transcode the video (regenerates `data/v/` — gitignored)

```bash
ffmpeg -i <clip>.mp4 -vf "fps=15,scale=528:296:flags=lanczos" \
  -qscale:v 8 -start_number 0 panel/data/v/%03d.jpg
```

The total must fit the ~4.9 MB LittleFS partition. 15fps matches the panel's
2-refresh cadence — bumping fps/quality/size risks judder or overflow.

## Board pin map & MQ-2 wiring

The 800×480 RGB panel consumes ~20 GPIOs, so free pins are scarce.

| Function | GPIO |
|---|---|
| RGB DE / VSYNC / HSYNC / PCLK | 5 / 3 / 46 / 7 |
| RGB R / G / B | R 1,2,42,41,40 · G 39,0,45,48,47,21 · B 14,38,18,17,10 |
| **I²C** (touch + CH422G + sensor) | **SDA 8 / SCL 9** |
| Spare ADC1 pin | **6** |
| microSD (SPI) | MOSI 11 / SCK 12 / MISO 13 |

**MQ-2** is analog and needs a 5V heater, and the panel eats every easy ADC
pin. Two options:
- `AOUT → GPIO 6` (the one free ADC1 channel — if your board breaks it out;
  works while WiFi is on, unlike the ADC2 pins), or
- an **ADS1115** I²C ADC on the SDA8/SCL9 connector, `AOUT → ADS1115 A0`.

Power the heater from **5V / VBUS**, common **GND**.
