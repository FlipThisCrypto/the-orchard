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
└── functions/
    └── fw/
        └── [[path]].js                    # Cloudflare Pages CORS proxy for release assets
```

Firmware blobs are **not committed here.** `manifest.json` points at the merged
`*-web-*.bin` assets on the matching
[GitHub Release](https://github.com/FlipThisCrypto/the-orchard/releases) (built
by `.github/workflows/release.yml` on each `v*` tag). This stops the repo from
growing ~2 MB per firmware version. Those `*-web-*.bin` are full-flash blobs
(bootloader + partitions + boot_app0 + app), ready to flash at offset `0x0`; the
plain `*.bin` assets are app-only images for OTA.

### The CORS proxy (why `manifest.json` points at `/fw/...`)

esp-web-tools runs in the browser and `fetch()`es each `.bin`. **GitHub
release-asset downloads do _not_ send an `Access-Control-Allow-Origin`
header** (verified: both the `github.com/...releases/download/...` redirect and
the final `release-assets.githubusercontent.com` response omit it), so a direct
cross-origin fetch from the flasher page is blocked and the install fails. This
is true no matter where the page itself is hosted.

So the manifest points at **same-origin** paths — `/fw/<tag>/<file>.bin` —
served by the Cloudflare Pages Function in `functions/fw/[[path]].js`. That
function re-streams the release asset with permissive CORS, validated to this
repo's releases and `.bin` files only (not an open proxy). When the flasher is
hosted on Cloudflare Pages, the function deploys automatically with the static
site — no separate service.

**Bumping the flashed version:** after tagging a release, update the two
`builds[].parts[].path` tags (`/fw/vX.Y.Z/...`) and the `version` field in
`manifest.json` to the new tag. The proxy needs no change — it resolves any
valid tag. (The full-flash merge recipe lives in the release workflow.)

**Validate before deploy:**

```bash
# Shape only (no network):
python tools/verify_flasher_manifest.py --offline
# Confirm each release asset exists on GitHub:
python tools/verify_flasher_manifest.py
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

The `/fw/...` blobs are served by a Cloudflare Pages **Function**, so a plain
static server won't serve them. Use Wrangler's local Pages runtime, which runs
the function too:

```bash
npx wrangler pages dev flasher
# Opens http://localhost:8788/ with Functions active.
# Click "Install Tree Firmware", pick the board's COM port, watch it flash.
```

A plain `python -m http.server` from `flasher/` is still fine for iterating on
the page's HTML/CSS, but the install itself will `404` on `/fw/...` because the
function isn't running. Web Serial requires `localhost` or `https://`, so either
localhost URL works; `http://192.168.x.x:...` from a phone wouldn't.

## Build a fresh firmware blob

> **This is now automated.** `.github/workflows/release.yml` runs exactly the
> merge below for all envs on every `v*` tag and uploads the `*-web-*.bin`
> assets the manifest points at. The manual steps here are for local testing
> or reproducing/auditing a release artifact.

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

# 3. Flash this merged blob directly with esptool for a local bench test,
#    e.g.  python -m esptool --chip esp32 write_flash 0x0 <output>.bin
#    (manifest.json does NOT point at this local file — it references the
#    released asset through the /fw/<tag>/ proxy, so the web installer always
#    flashes what the release workflow published, not an un-tagged local build.)
```

The `<VERSION>` string should match `ORCHARD_FIRMWARE_VERSION` in `firmware/platformio.ini` (and `firmware/include/version.h` if you bumped it). To make the **web installer** serve a new version, cut a `v<VERSION>` release (the workflow uploads the `*-web-*.bin` assets) and bump the tag in the `manifest.json` `/fw/...` paths — see "Bumping the flashed version" above.

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

The matching `manifest.json` `builds[]` entry (already present) points at the
released asset through the proxy:

```json
{
  "chipFamily": "ESP32-S3",
  "parts": [
    { "path": "/fw/v<VERSION>/orchard-freenove_esp32s3_uart-web-v<VERSION>.bin", "offset": 0 }
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

> **Whatever you pick must serve the `/fw/...` proxy** (see "The CORS proxy"
> above), or the firmware download is CORS-blocked in the browser and the
> install fails. Only Cloudflare Pages runs the bundled function out of the
> box; the other options need the proxy supplied another way.

### Option A — Cloudflare Pages (recommended)

Connect the repo to Cloudflare Pages with the project (build output) directory
set to `flasher/`, then attach a custom domain such as `flash.theorchard.network`
(a CNAME Cloudflare wires up for you). URL becomes
`https://flash.theorchard.network/`, it auto-redeploys on every push to `main`,
**and the `functions/fw/[[path]].js` CORS proxy deploys automatically** — no
separate service, no extra config. This is the only zero-extra-work option.

### Option B — GitHub Pages (static only — needs the proxy elsewhere)

Repo settings → Pages → Deploy from branch `main`, folder `/flasher`. URL
becomes `https://flipthiscrypto.github.io/the-orchard/`. Works for the page
itself, but **GitHub Pages can't run the Pages Function**, so `/fw/...` 404s and
the install fails unless you front it with a CORS proxy (e.g. a standalone
Cloudflare Worker running the same logic) and repoint `manifest.json` at that
proxy's origin. Not recommended unless you already have such a proxy.

### Option C — Same host as the Oracle (Phase 9)

When the hosted Oracle exists at `oracle.theorchard.network`, the flasher can
live at `theorchard.network/flash` on the same reverse proxy. You'd add a
location that proxies `/fw/<tag>/<file>` to the GitHub release download and
injects `Access-Control-Allow-Origin` (the same job `functions/fw/[[path]].js`
does) — then repoint `manifest.json` if the path differs. Adds no infra beyond
the host that already exists, at the cost of writing that proxy rule yourself.

### Option C — Same VPS as the hosted Oracle (Phase 9)

When the hosted Oracle exists at `oracle.theorchard.network`, the flasher can live at `theorchard.network/flash` served by the same Caddy/nginx instance. Adds zero infra cost beyond the VPS that already exists.

## Security note

The `.bin` in this directory IS the production firmware blob. It's checked into the public repo, so anyone can audit it. **No secrets are baked into the firmware** — node id, signing key, WiFi credentials, and the Oracle URL are all generated/configured at runtime via NVS during the dashboard's Plant a Tree wizard.

A new operator who flashes via this page gets a freshly-keyed Tree. Two operators flashing the same blob get two different node ids and two different HMAC secrets, because both are generated by `esp_random()` on first boot and persisted in NVS.
