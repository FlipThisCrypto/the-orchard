# flasher/ — Browser-based firmware installer for Trees

Static site that flashes Orchard Tree firmware to a plugged-in ESP32 board from a webpage — no PlatformIO, no toolchain, no terminal. Uses [esp-web-tools](https://esphome.github.io/esp-web-tools/) (which wraps esptool-js + the Web Serial API).

## Why this exists

The [Operator Quickstart](../docs/OPERATOR_QUICKSTART.md) requires installing PlatformIO and a ~200 MB ESP32 toolchain just to flash one board. That's a hard ask for non-developer operators. This page reduces firmware install to: plug in board → click button → wait 30 seconds → done.

ESPHome, Tasmota, Adafruit's WipperSnapper, and Espressif's own [esp-launchpad](https://espressif.github.io/esp-launchpad/) all use this exact pattern. It's the modern, novice-friendly path.

## What's in the directory

```
flasher/
├── README.md                              (this file)
├── index.html                             # the install page
├── manifest.json                          # esp-web-tools build descriptor
├── wroom32u/
│   └── orchard-wroom32u-0.4.7.bin         # classic ESP32 (WROOM-32U)
└── freenove-s3-uart/
    └── orchard-s3-uart-0.4.7.bin          # ESP32-S3 w/ CH343 UART bridge
                                           # both are merged blobs (bootloader
                                           # + partitions + boot_app0 + app),
                                           # ready to flash at offset 0x0.
```

esp-web-tools reads the connected chip's family and automatically picks
the matching `builds[]` entry from `manifest.json` (ESP32 → WROOM image,
ESP32-S3 → S3 image), so one install button serves both boards.

`index.html` loads [`esp-web-tools`](https://www.npmjs.com/package/esp-web-tools) from unpkg CDN — no build step, no `npm install`. Open `index.html` in any Chromium-based browser served over HTTPS or `http://localhost` and it works.

## Browser support

| Browser | Web Serial | Works |
|---|---|---|
| Chrome 89+ | ✅ | yes |
| Edge 89+ | ✅ | yes |
| Brave | ✅ | yes |
| Opera | ✅ | yes |
| Firefox | ❌ | no — no Web Serial API |
| Safari  | ❌ | no — no Web Serial API |

Mobile browsers generally don't expose Web Serial either. Desktop only for now.

## Test locally

```bash
cd flasher
python -m http.server 8088
# Open http://localhost:8088/ in Chrome/Edge.
# Click "Install Tree Firmware", pick the board's COM port, watch it flash.
```

Web Serial requires `localhost` or `https://`, so `http://localhost:8088` works but `http://192.168.x.x:8088` from a phone wouldn't.

## Build a fresh firmware blob

After any firmware change, rebuild + re-merge:

```bash
# 1. Compile the WROOM-32U target with PlatformIO
cd firmware
python -m platformio run -e freenove_esp32_wroom

# 2. Merge bootloader + partitions + boot_app0 + app into a single
#    file that esp-web-tools can flash to offset 0x0.
cd ..
# --flash-mode/--flash-freq = keep: preserve exactly what the env baked
# into bootloader.bin (the WROOM env forces dio/40m). "keep" can't drift
# out of sync with platformio.ini the way hardcoded values can.
python -m esptool --chip esp32 merge_bin \
    --output flasher/wroom32u/orchard-wroom32u-<VERSION>.bin \
    --flash-mode keep --flash-freq keep --flash-size 4MB \
    0x1000  firmware/.pio/build/freenove_esp32_wroom/bootloader.bin \
    0x8000  firmware/.pio/build/freenove_esp32_wroom/partitions.bin \
    0xe000  ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin \
    0x10000 firmware/.pio/build/freenove_esp32_wroom/firmware.bin

# 3. Update flasher/manifest.json:
#       "version": "<VERSION>"
#       "parts": [{ "path": "wroom32u/orchard-wroom32u-<VERSION>.bin", ... }]
```

The `<VERSION>` string should match `ORCHARD_FIRMWARE_VERSION` in `firmware/platformio.ini` (and `firmware/include/version.h` if you bumped it).

## ESP32-S3 build

Shipped as of 0.4.7 (`freenove-s3-uart/orchard-s3-uart-0.4.7.bin`).
`esp-web-tools` auto-detects the connected chip and picks the matching
`builds[]` entry, so the same install button serves both boards.

We ship the **`freenove_esp32s3_uart`** variant (Arduino `Serial` over the
external CH343 UART bridge) because that's what the S3 Trees in the field
use. A board with native USB-CDC instead would need the `freenove_esp32s3`
env's image — but esp-web-tools keys on chip *family* (both report
"ESP32-S3"), so only one S3 image can be live at a time.

To regenerate after a firmware change:

```bash
python -m platformio run -e freenove_esp32s3_uart
python -m esptool --chip esp32s3 merge_bin \
    --output flasher/freenove-s3-uart/orchard-s3-uart-<VERSION>.bin \
    --flash-mode keep --flash-freq keep --flash-size 8MB \
    0x0     firmware/.pio/build/freenove_esp32s3_uart/bootloader.bin \
    0x8000  firmware/.pio/build/freenove_esp32s3_uart/partitions.bin \
    0xe000  ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin \
    0x10000 firmware/.pio/build/freenove_esp32s3_uart/firmware.bin
```

The matching `manifest.json` `builds[]` entry (already present):

```json
{
  "chipFamily": "ESP32-S3",
  "parts": [
    { "path": "freenove-s3-uart/orchard-s3-uart-<VERSION>.bin", "offset": 0 }
  ]
}
```

(Note ESP32-S3's bootloader sits at offset `0x0`, not `0x1000` like classic
ESP32 — that's the one material difference in the offset map above.)

> ⚠️ The S3 web-flasher image is newly added in 0.4.7 and has not yet had an
> end-to-end smoke test through the browser installer. The CLI path
> (`pio run -e freenove_esp32s3_uart -t upload`) is the verified one — flash
> one S3 through the web page and confirm it boots before relying on it.

## Publishing

Three production options, in increasing order of "you control the URL":

### Option A — GitHub Pages (free, zero setup)

```bash
# In repo settings → Pages → Source: Deploy from branch
# Branch: main
# Folder: /flasher
```

URL becomes `https://flipthiscrypto.github.io/the-orchard/`. Works immediately, no DNS to manage, no other infra. The downside: the URL is long and brand-foreign.

### Option B — Cloudflare Pages (free, custom domain)

Connect the repo to Cloudflare Pages, set the build output directory to `flasher/`, point a CNAME at `flash.theorchard.network` (or whatever subdomain you settle on). URL becomes `https://flash.theorchard.network/`. Auto-redeploys on every push to main.

### Option C — Same VPS as the hosted Oracle (Phase 9)

When the hosted Oracle exists at `oracle.theorchard.network`, the flasher can live at `theorchard.network/flash` served by the same Caddy/nginx instance. Adds zero infra cost beyond the VPS that already exists.

## Security note

The `.bin` in this directory IS the production firmware blob. It's checked into the public repo, so anyone can audit it. **No secrets are baked into the firmware** — node id, signing key, WiFi credentials, and the Oracle URL are all generated/configured at runtime via NVS during the dashboard's Plant a Tree wizard.

A new operator who flashes via this page gets a freshly-keyed Tree. Two operators flashing the same blob get two different node ids and two different HMAC secrets, because both are generated by `esp_random()` on first boot and persisted in NVS.
