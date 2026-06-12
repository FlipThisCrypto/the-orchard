# firmware/ — Tree firmware (classic ESP32 + ESP32-S3)

Modular firmware for a **Tree** — a deployed node in The Orchard. Built with PlatformIO and the Arduino-ESP32 framework. Supports **two build targets**:

| Build env             | Chip                                       | Notes                                                                                       |
|-----------------------|--------------------------------------------|---------------------------------------------------------------------------------------------|
| `freenove_esp32_wroom` *(default)* | Classic ESP32 (WROOM-32 / WROOM-32U) | The v1 prototype board. 4MB flash, no native USB, Serial via USB-UART bridge. LED on GPIO 2.   |
| `freenove_esp32s3`    | ESP32-S3 (Freenove S3 dev board)           | Newer/optional. 8MB flash, native USB-CDC, LED on GPIO 48.                                  |

```bash
pio run                                           # builds the default (WROOM)
pio run -e freenove_esp32s3                       # build for S3
pio run -e freenove_esp32_wroom -t upload --upload-port COM4   # flash a WROOM
pio run -e freenove_esp32s3 -t upload --upload-port COM7        # flash an S3
```

> **Vocabulary:** In code, the unit is a `node`; in user-facing copy, it's a **Tree**. See the [Glossary](../README.md#glossary).

## What's here (Phase 2)

```
firmware/
├── platformio.ini             # board + framework + libs
├── partitions.csv             # 8MB OTA-capable partition table
├── include/
│   ├── pins.h                 # pin allocation (overridable via -D flags)
│   ├── config.h               # build-time defaults (sample interval, etc.)
│   └── version.h              # firmware version string
└── src/
    ├── main.cpp               # setup() / loop()
    ├── identity.{h,cpp}       # node_id + HMAC signing secret (NVS-stored)
    ├── sensors/
    │   ├── sensor.h           # base Sensor interface + SensorRegistry
    │   ├── sensor_registry.cpp
    │   ├── mq135.{h,cpp}      # MQ-135 analog air-quality driver
    │   └── gps_neo.{h,cpp}    # NEO-6M/7M/8M GPS UART driver (TinyGPSPlus)
    └── net/
        ├── wifi_mgr.{h,cpp}   # WiFi connect + NVS-stored creds
        ├── oracle.{h,cpp}     # signed POST to the oracle
        ├── ota.{h,cpp}        # HTTP /ota + /health server
        └── serial_console.{h,cpp}  # USB-serial provisioning console
```

**Not in this Phase 2 commit (deferred until hardware is wired):**

- AHT20 / BMP280 / BH1750 / PMS5003 drivers — use the [`examples/sensor_driver/template`](../examples/sensor_driver/) as the starting point and add them when each sensor lands on the board.

## Design principles

- **Self-registering sensors.** Each driver declares a static `AutoRegister<>` instance that pushes itself into a global registry at startup. Adding a sensor = drop in a `.cpp` + `#include "sensor.h"` + one static registration line. No central if-else chain.
- **Active vs registered.** `begin()` returns `false` if the sensor isn't physically present; the registry marks it inactive and silently skips it during sampling. This is how I2C / UART probing decides whether to include a sensor.
- **Resilient.** WiFi reconnects on failure. OTA never bricks the device (standard Arduino-ESP32 OTA partition table; `Update` library). If the oracle is unreachable the next sample just tries again. (Persistent on-device buffering is a v1.1 add.)
- **Signed.** Every POST includes an `X-Orchard-Sig` HMAC-SHA256 over the request body. The signing secret is generated on first boot, stored in NVS, never transmitted over the network, and exposed only to the dashboard over the local USB-serial link (via the `KEY` console command, used once at registration time).
- **Modular radios.** WiFi today. LoRa / cellular interfaces will slot into `src/net/` without touching sensors.

## v1 signing scheme

HMAC-SHA256 with a shared 32-byte secret. The Tree generates the secret on first boot and shows it to the dashboard over USB-serial during registration; the dashboard then registers `(node_id, hmac_secret)` with the oracle, which from that point onward can verify any submission.

> **Asymmetric device keys (ADR-0007):** each Tree also carries a secp256r1 (P-256) keypair — generated on first boot, scalar in NVS, public key exported via `PUBKEY`/`HW_INFO`. Signing is RFC 6979 deterministic ECDSA over sha256 via mbedTLS, which is bundled with ESP-IDF — no extra crypto library (unlike the briefly-adopted ed25519, which CLVM also cannot verify). HMAC remains the v1 transport auth to the oracle; the P-256 signature is what makes readings third-party-verifiable ([ADR-0003](../docs/decisions/0003-datalayer-verifiable-dataset.md)) and on-chain-verifiable via `secp256r1_verify` ([ADR-0008](../docs/decisions/0008-serverless-target-architecture.md)).

## Build / flash

Prereqs:

- [PlatformIO Core](https://platformio.org/install/cli) (CLI) or the VS Code extension.
- A USB-C cable to your Freenove ESP32-S3.
- The board powered on and visible as a COM port.

```bash
cd firmware
pio run                     # build
pio run -t upload           # flash over USB
pio device monitor          # serial console at 115200
```

On first boot you'll see:

```
=== The Orchard — Tree firmware ===
fw=0.1.0 node_id=<32 hex chars>
Type 'STATUS' for current state, 'PING' to test.
[identity] generated new node id
[identity] generated new signing secret
[sensors] mq135        bus=2 active=yes
[sensors] gps          bus=1 active=yes
[sensors] 2 active sensor(s)
[wifi] no creds stored; idle. Use WIFI_SET over serial.
```

## Provisioning the Tree (over USB serial)

The dashboard does this automatically in Phase 4. Until then, you can drive it by hand from `pio device monitor`:

```text
PING                            -> OK pong
NODE_ID                         -> OK <hex node id>
KEY                             -> OK <hex 32-byte signing secret>
WIFI_SET YourSSID YourPassword  -> OK
ORACLE_SET http://192.168.1.10:8000/readings
                                -> OK
STATUS                          -> OK {...}
SAMPLE_NOW                      -> OK sampling
I2C_SCAN                        -> OK 0x76 0x77 ...    (or "OK (no devices)")
GPS_RAW                         -> OK gps_raw_start
                                   $GPGSV,...          (3 seconds of raw NMEA)
                                   ...
                                   OK gps_raw_end
REBOOT                          -> OK rebooting
```

`I2C_SCAN` and `GPS_RAW` are diagnostic commands — run them when a sensor isn't reading and you need to verify the wires are actually carrying the right thing.

The `KEY` command prints the signing secret in plaintext over USB. **Only do this while the Tree is physically wired to your dashboard PC**, never over a network link, never to a public chat.

## What the oracle receives

Each sample POST is a small JSON document:

```json
{
  "node_id": "A3F2...",
  "fw": "0.1.0",
  "ts_ms": 12345678,
  "sensors": {
    "mq135": { "adc_raw": 1834.0, "adc_baseline": 1820.3,
               "adc_dev": 13.7, "voltage_v": 1.477 },
    "gps":   { "satellites": 7, "fix": true, "fix_age_ms": 412,
               "lat": 38.004616, "lon": -85.737403,
               "alt_m": 168.2, "speed_kmh": 0.0,
               "utc": "2026-05-27T20:02:14Z" }
  }
}
```

Headers:

```
Content-Type: application/json
X-Orchard-Node: <hex node id>
X-Orchard-Sig:  <hex hmac-sha256 of body>
```

## OTA updates

The Tree listens on `http://<tree-ip>:80` with two endpoints:

- `GET /health` — `{ "node_id": "...", "fw": "...", "uptime_ms": ... }`
- `POST /ota` — multipart firmware binary upload; device flashes and reboots.

To do a manual OTA push:

```bash
curl -F "firmware=@.pio/build/freenove_esp32s3/firmware.bin" \
     http://<tree-ip>/ota
```

The dashboard (Phase 4) wraps this in a UI.

> **Don't expose the OTA port to the open internet.** There's no auth on `/ota` in v1 — it's intended for the LAN only.

## Status

Phase 2 ✅ Initial firmware committed. Two active sensor drivers (MQ-135, GPS), WiFi + OTA, signed POSTs, USB-serial provisioning. Ready for the oracle (Phase 3) to receive data.
