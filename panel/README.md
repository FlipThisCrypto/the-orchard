# panel/ — The Orchard status panel (Phase 9.2)

A physical, touch-screen status panel for The Orchard, built on the
**Waveshare ESP32-S3-Touch-LCD-4.3 / 4.3B** (800×480 RGB capacitive touch).

It plays a short looping clip, then (tap the screen) shows live **Orchard
network stats** pulled from your oracle, then (tap again) a live **MQ-2 gas
sensor** reading. Think "Orchard View, but a thing you can hang on the wall."

> This is a **separate project** from `firmware/` (the Tree sensor node).
> The panel is a display/kiosk, not a Tree — different board, different
> libraries, different job.

## Build phases

| Phase | What | Status |
|---|---|---|
| **1. Bring-up** | CH422G backlight + RGB panel + splash | ✅ this commit — flash & confirm the screen lights up |
| **2. Video** | transcode `.mp4` → MJPEG → LittleFS → play loop | next |
| **3. Stats** | WiFi → oracle `/network/stats`; GT911 touch toggles video ↔ stats ↔ sensor | |
| **4. Sensor** | MQ-2 gas sensor live on screen | |

## Board pin map (Waveshare 4.3 / 4.3B)

The 800×480 RGB panel consumes ~20 GPIOs, so free pins are scarce.

| Function | GPIO |
|---|---|
| RGB DE / VSYNC / HSYNC / PCLK | 5 / 3 / 46 / 7 |
| RGB R0-R4 | 1, 2, 42, 41, 40 |
| RGB G0-G5 | 39, 0, 45, 48, 47, 21 |
| RGB B0-B4 | 14, 38, 18, 17, 10 |
| **I²C (touch GT911 + CH422G + your sensor)** | **SDA 8 / SCL 9** |
| Touch INT | 4 |
| Backlight / LCD reset / touch reset | via CH422G expander (EXIO2 / EXIO3 / EXIO1) |
| microSD (SPI) | MOSI 11 / SCK 12 / MISO 13 / CS via CH422G |
| Spare ADC1 pin | **6** (the one free analog-capable pin) |
| CAN / RS485 | 15,16 / 43,44 |

### MQ-2 gas sensor wiring (Phase 4)

The MQ-2 is **analog** + needs a **5V heater**, which is awkward here because
the panel eats every easy ADC pin and the I²C connector is 3V3/digital:

- **Preferred:** MQ-2 `AOUT → GPIO 6` (the one free ADC1 channel; works while
  WiFi is on, unlike the ADC2 pins). Needs GPIO 6 broken out on a pad/header.
- **If GPIO 6 isn't exposed:** an **ADS1115** I²C ADC on the I²C connector
  (SDA 8 / SCL 9); MQ-2 `AOUT → ADS1115 A0`. Full analog, no scarce GPIO.
- Power the heater from **5V / VBUS** (~150 mA), common **GND**. Don't run it
  off a weak rail — a hungry MQ heater browning the radio is a real failure
  mode (see `docs/LOG.md`).
- The MQ-2 also has a digital `DOUT` (pot-set threshold) if you only want a
  "gas detected" trip instead of a gradient — that needs only any free pin.

## Flash it (Phase 1)

Native USB-C. If auto-reset doesn't drop it into download mode, hold **BOOT**,
tap **RST**, release **BOOT**, then run upload.

```powershell
cd "I:\DeMeter Data\Chia DePIN\panel"
python -m platformio run -e waveshare_s3_touch_43b -t upload --upload-port COM<n>
python -m platformio device monitor -e waveshare_s3_touch_43b --port COM<n>
```

### What success looks like

- Serial prints `flash=8MB psram=8MB`, `[gfx] begin ok`, `[gfx] splash drawn`.
- The panel shows **7 color bars** across the top and **"THE ORCHARD"** in
  white with a green status line.

If the bars/text render → the display stack works, on to Phase 2. If serial
says `begin ok` but the screen is **dark**, the **CH422G backlight init** in
`src/main.cpp` is the thing to tweak first (it's flagged in the code).
