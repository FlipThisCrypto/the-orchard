# Operator Quickstart

> **Plant your first Tree, end-to-end.** Hardware in a box → first signed reading on the dashboard. ~60 minutes for someone comfortable with a terminal, ~3 hours if every step is new.

---

## What this gets you

By the end of this doc you'll have:

1. **One Tree** — an ESP32-class device with sensors, running the Orchard firmware, posting signed environmental readings every 60 seconds.
2. **A local Oracle** — a small Python service on your PC that receives those readings, verifies their signatures, and stores them.
3. **Orchard View** — a local web dashboard showing your Tree's live data.

This is the **single-operator** path — everything runs on your machine. It's how to verify the loop works end-to-end. A future doc (`OPERATOR_NETWORK.md`, not yet written) will cover joining a shared hosted network where multiple operators submit to one oracle and earn `$JUICE` for verified uptime.

> **Pre-alpha caveat:** The Orchard is a proof of concept. The pieces below work today, but the *network* (multiple operators sharing one oracle, automated `$JUICE` payouts to strangers, an NFT-gated registration UX) isn't built yet. You can plant a Tree, see it on a dashboard, and own the data — that's the v1 promise. The earning-`$JUICE`-as-a-stranger flow is Phase 10+.

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Hardware shopping list](#2-hardware-shopping-list)
3. [Identify your ESP32 board](#3-identify-your-esp32-board)
4. [Assemble the hardware](#4-assemble-the-hardware)
5. [Install the software](#5-install-the-software)
6. [Flash the firmware](#6-flash-the-firmware)
7. [First boot — what you should see](#7-first-boot--what-you-should-see)
8. [Run the Oracle](#8-run-the-oracle)
9. [Run Orchard View](#9-run-orchard-view)
10. [Plant your Tree](#10-plant-your-tree)
11. [Verify the first reading](#11-verify-the-first-reading)
12. [Troubleshooting](#12-troubleshooting)
13. [What's next](#13-whats-next)

---

## 1. Prerequisites

### Skills

- Comfortable with a terminal (PowerShell on Windows, bash on macOS/Linux).
- Can install Python and a few system packages.
- Can plug header pins into a breadboard. **No soldering required** for the reference build.

### Software you'll install

- **Python 3.11 or newer** — for the Oracle and Dashboard.
- **PlatformIO Core CLI** — for building and flashing the Tree firmware. (Installs as a Python package, ~150 MB once it pulls the ESP32 toolchain.)
- **A CP210x USB-UART driver** (Windows only, see [section 6](#6-flash-the-firmware)).
- **Git** — to clone the repo.

### Accounts

- A **GitHub account** is not required to use the project but is needed if you want to report issues or contribute.
- A **Chia wallet** is NOT required to plant your first Tree. It will be required once the public hosted network exists and you want to earn `$JUICE`. We'll cross that bridge in `OPERATOR_NETWORK.md`.

### Operating system

These steps are written for **Windows 11 PowerShell**. macOS and Linux work too — replace `pip install` paths and use `/dev/ttyUSB0`-style port names instead of `COM4`. Specific differences are noted inline.

---

## 2. Hardware shopping list

The reference build targets **classic ESP32 (WROOM-32U)**. The ESP32-S3 build is also supported (see [board identification](#3-identify-your-esp32-board)) but the parts list here assumes WROOM-32U because that's what's in the v1 prototype enclosure.

| Part | Why | Approximate cost (USD, 2026) | Where |
|---|---|---|---|
| **Freenove ESP32-WROOM-32U board** (with breakout) | The brain. External-antenna variant for better WiFi range in an enclosure. | $14 | [Freenove store](https://store.freenove.com/products/fnk0058), Amazon |
| **2.4 GHz WiFi/BT antenna with U.FL → RP-SMA pigtail** (~10 cm) | The WROOM-32U has no onboard antenna — you must plug one in or WiFi range is centimeters. | $6 | Amazon, AliExpress |
| **MQ-135 air quality sensor breakout** | Measures total VOC / CO₂-ish / NH₃-ish air quality. The "I can smell something is off" sensor. | $3 | Amazon (search "MQ-135 module") |
| **NEO-6M, 7M, or 8M GPS module** with active antenna | Location + UTC time. The active antenna with the magnetic puck and 1-3 m cable matters — the on-PCB chip antenna is useless indoors. | $12 | Amazon (search "NEO-6M GPS active antenna") |
| **USB-C cable** (or USB-A → micro depending on board variant) | Power + flashing. Must be a *data* cable, not a charge-only cable. | $4 | Anywhere |
| **Female-to-female jumper wires** (~20) | Connecting sensors to the breakout. | $5 | Amazon (search "Dupont jumper wires") |
| **Clear ABS waterproof enclosure** with cable gland | For outdoor deployment. Optional for first-Tree bring-up. | $10 | Amazon (search "ABS waterproof project box clear") |

**Total: ~$54** for the working reference build, less if you're parts-bin-rich.

### Optional sensors (drivers ready or planned)

- **BME280** (temp + humidity + pressure, I2C) — driver shipped. $4. Highly recommended; gives you a real weather story per Tree.
- **AHT20**, **BMP280**, **BH1750**, **PMS5003** — driver templates in `examples/sensor_driver/`. Add when you want to extend.

### What you should NOT buy yet

- An **Orchard Pass NFT** — there's no public sale yet. Genesis Passes are held by the project; a public sale mechanism is Phase 10.
- A **Chia node** — you don't need to run a full node to plant a Tree. Only Phase 7 ($JUICE payout) needs one, and that's the project operator's role for now.

---

## 3. Identify your ESP32 board

The firmware supports two targets. Look at your board and pick:

### Classic ESP32 (WROOM-32 / WROOM-32U) — default

- Two rows of pins, ~18 per side.
- Big silver metal can labeled `ESP32-WROOM-32` or `ESP32-WROOM-32U`.
- The **U variant** has a small **U.FL antenna connector** (gold square, ~3 mm) at one corner. Without it, an antenna is meant to be soldered to onboard pads.
- USB → micro or USB-C via a CP210x bridge chip. Programming is at ~921 kbaud.
- This is the v1 prototype board.

PlatformIO env: `freenove_esp32_wroom` (default, no flag needed).

### ESP32-S3 (Freenove S3 dev board) — optional

- Slimmer board, native USB-C, no separate USB-UART chip.
- Onboard RGB status LED.
- 8 MB flash.
- Slightly newer chip, more RAM, native USB.

PlatformIO env: `freenove_esp32s3` (use `-e freenove_esp32s3` on every command).

**Throughout this doc, commands assume the default WROOM-32U.** If you're on S3, append `-e freenove_esp32s3` to every `pio run` command. Pin assignments differ slightly between the two — see [Assemble the hardware](#4-assemble-the-hardware).

---

## 4. Assemble the hardware

### Pin map (WROOM-32U on Freenove breakout)

The Freenove breakout has **two labels per hole**: one column for ESP32 (classic), one for ESP32-S3. The labels are slightly offset. **Always read the column matching the chip in your hand.** This is the single biggest gotcha for the v1 prototype.

| Wire to | ESP32-WROOM-32U pin | Freenove breakout label | Notes |
|---|---|---|---|
| MQ-135 VCC | 5V or 3V3 rail | `5V` | 5V gives a slightly better dynamic range; 3V3 works too |
| MQ-135 GND | GND rail | `GND` | Any GND |
| MQ-135 AO (analog out) | GPIO 34 | `34` | Input-only ADC pin; perfect for MQ-135 |
| MQ-135 DO (digital threshold) | leave disconnected | — | We use the analog reading |
| GPS VCC | 5V rail | `5V` | NEO-6M wants 3.3V *or* 5V depending on board; check yours, most accept 5V via onboard LDO |
| GPS GND | GND rail | `GND` | Any GND |
| **GPS TX → ESP32 RX** | GPIO 18 | `18` | Critical: this is the ESP32-side column on the Freenove breakout, NOT the S3-side hole one below it |
| **GPS RX ← ESP32 TX** | GPIO 19 | `19` | Same — ESP32 column |
| WiFi/BT antenna | U.FL connector | (on the corner of the WROOM module) | Snap fits with a tactile *click*; pull straight up to remove |

### Pin map (ESP32-S3)

| Wire to | ESP32-S3 pin |
|---|---|
| MQ-135 AO | GPIO 34 |
| GPS TX → ESP32 RX | GPIO 4 |
| GPS RX ← ESP32 TX | GPIO 5 |

### Antenna install (WROOM-32U only)

1. Find the U.FL connector — small gold square on the corner of the WROOM module.
2. Align the U.FL plug on your pigtail (it's directional — flat side down).
3. Press straight down with a fingernail until you feel a tactile *click*. Never press sideways — the connector breaks easily.
4. Screw the other end (RP-SMA) into your antenna.
5. Route the antenna **outside** any metal enclosure when you deploy.

Without an antenna, the WROOM-32U has ~1 m of effective WiFi range. With one, you should see -50 to -60 dBm RSSI in a typical home setup.

### Optional BME280 (temperature + humidity + pressure, I2C)

| Wire to | ESP32 pin (WROOM and S3) |
|---|---|
| VCC | 3.3V (most BME280 breakouts) |
| GND | GND |
| SDA | GPIO 21 |
| SCL | GPIO 22 |

The firmware auto-detects whether it's at I2C address 0x76 or 0x77.

### Optional DS18B20 (1-Wire temperature probe)

The classic 3-wire waterproof temperature probe with the long cable. Often comes with a 4.7 kΩ resistor pre-wired in a heat-shrink tube near the connector — if yours does, you're good. If yours is a bare TO-92 chip, you'll need to add the resistor yourself.

| Wire to | ESP32 pin (WROOM and S3) | Probe wire color (typical) |
|---|---|---|
| VCC | 3.3V (NOT 5V — DS18B20 is 3.3V) | Red |
| GND | GND | Black |
| DATA | GPIO 25 | Yellow |
| **4.7 kΩ resistor** | between DATA and 3.3V | — |

The pull-up resistor is **not optional**. Without it the 1-Wire bus floats and the chip never responds — the boot log will show `bme280 active=no` *and* `ds18b20 active=no` regardless of how many sensors you've actually connected. If you flash a Tree with a DS18B20 wired but no pull-up, the firmware prints a hint to serial:

```
[ds18b20] no devices on 1-Wire bus — check 4.7k pull-up
between DATA and VCC, and DATA wire on the configured GPIO.
```

Multiple DS18B20s can share one data wire (1-Wire bus). v1 firmware reads the first one it finds; the JSON payload includes `device_count` so you can see when more are physically connected than the driver is reading.

### Sanity check before powering on

- No VCC wire shorted to GND.
- No two outputs wired together.
- USB cable is plugged into the ESP32 board, not directly into a sensor.
- Antenna is connected (WROOM-32U). Without one, your WiFi will *appear* dead.

---

## 5. Install the software

### Windows

```powershell
# 1. Python 3.11+
# Install from https://www.python.org/downloads/  (check "Add Python to PATH")
python --version    # should print 3.11.x or newer

# 2. Git
# Install from https://git-scm.com/download/win  (defaults are fine)
git --version

# 3. CP210x USB driver (WROOM-32U boards use the CP2102 USB-UART bridge)
# Download from:
#   https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers
# Run the installer. Reboot is usually NOT required, but if Device
# Manager doesn't see your board as "Silicon Labs CP210x USB to UART
# Bridge (COM4)" or similar after plugging in, reboot once.

# 4. PlatformIO CLI
python -m pip install --upgrade pip
python -m pip install platformio
pio --version
```

If `pio --version` fails because PATH wasn't updated, restart PowerShell.

### macOS / Linux

```bash
# Python via Homebrew (macOS) or apt (Linux)
brew install python git              # macOS
# OR
sudo apt install python3 python3-pip python3-venv git   # Debian/Ubuntu

# CP210x driver is built into recent macOS and Linux kernels — no install.
# The board appears as /dev/cu.SLAB_USBtoUART (macOS) or /dev/ttyUSB0 (Linux).

# PlatformIO CLI
python3 -m pip install --user platformio
pio --version
```

### Clone the repo and install Python deps

```powershell
cd C:\Users\YourName\Projects       # wherever you keep code
git clone https://github.com/FlipThisCrypto/the-orchard.git
cd the-orchard

python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows
# source .venv/bin/activate         # macOS / Linux

# Hash-pinned install (safe against supply-chain attacks):
pip install --require-hashes -r oracle/requirements.lock `
            --require-hashes -r dashboard/requirements.lock `
            --require-hashes -r orchard_chia/requirements.lock
```

> **PowerShell execution policy**: if `.venv\Scripts\Activate.ps1` errors with "running scripts is disabled," run once: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` and retry.

---

## 6. Flash the firmware

### Plug the board in

1. USB cable from your PC to the ESP32 board.
2. Confirm a new COM port appears:

   ```powershell
   Get-PnpDevice -Class Ports -PresentOnly |
     Where-Object { $_.FriendlyName -like '*CP210*' -or $_.FriendlyName -like '*USB*' } |
     Select-Object FriendlyName, Status
   ```

   You should see something like `Silicon Labs CP210x USB to UART Bridge (COM4)` with Status `OK`.

3. Note the COM number — you'll need it for the upload command.

### Build and flash

```powershell
cd firmware

# Build only — confirms toolchain and source compile
pio run

# Flash over USB.  Replace COM4 with your port.
pio run -t upload --upload-port COM4
```

First build pulls the ESP32 toolchain (~200 MB) — this takes a few minutes on first run, then is cached forever.

If you're on an S3 board:

```powershell
pio run -e freenove_esp32s3
pio run -e freenove_esp32s3 -t upload --upload-port COM7
```

### WROOM-32U boot-mode note

Some Freenove WROOM-32U boards (especially the breakout-version) don't auto-enter download mode. If `pio run -t upload` hangs on `Connecting....`:

1. **Hold the BOOT button** (the one labeled `BOOT` or `IO0`).
2. **Tap the RESET / EN button** while still holding BOOT.
3. **Release BOOT** when you see `Connecting....` succeed.

Wait for `Hash of data verified` then `Hard resetting via RTS pin...`. Once you've flashed successfully once, OTA (over-the-air) updates from the dashboard work over WiFi and you'll rarely use USB again.

---

## 7. First boot — what you should see

Open the serial monitor:

```powershell
pio device monitor --baud 115200 --port COM4
```

You should see:

```
=== The Orchard — Tree firmware ===
fw=0.1.0 node_id=A3F2C1D7...    (32 hex chars unique to your board)
Type 'STATUS' for current state, 'PING' to test.
[identity] generated new node id           (first boot only)
[identity] generated new signing secret    (first boot only)
[sensors] mq135        bus=2 active=yes
[sensors] gps          bus=1 active=yes
[sensors] 2 active sensor(s)
[wifi] no creds stored; idle. Use WIFI_SET over serial.
```

**Quick checks you can run from the monitor** (type the command and hit Enter):

| Command | Expected | What it tells you |
|---|---|---|
| `PING` | `OK pong` | Console works |
| `STATUS` | `OK {"fw":"0.1.0",...}` | Tree is alive |
| `I2C_SCAN` | `OK 0x76` (if you wired a BME280) or `OK (no devices)` | I2C wiring health |
| `GPS_RAW` | 3 seconds of `$GPGSV,...`, `$GPRMC,...` lines | GPS UART wiring health — silence here means TX/RX swapped or GPS unpowered |

If `GPS_RAW` is silent: re-check that GPS TX goes to ESP32 RX (GPIO 18 on WROOM, GPIO 4 on S3) and not the other way around. The most common bring-up bug.

Press `Ctrl+]` to exit the serial monitor.

---

## 8. Run the Oracle

In a new PowerShell window:

```powershell
cd C:\path\to\the-orchard
.venv\Scripts\Activate.ps1

# Copy the example env (you'll edit it once you know your LAN IP)
copy oracle\.env.example oracle\.env

# Start the oracle (defaults to 127.0.0.1:8000)
python -m oracle.app.main
```

You should see:

```
INFO:     Started server process [12345]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Open `http://127.0.0.1:8000/` in a browser — you should get a small JSON response confirming the service is up.

**Leave this window running.** The Oracle dies when you close it.

### Allowing the Tree to reach the Oracle

The Oracle binds to **127.0.0.1** (loopback) by default — only your PC can reach it. For your Tree on WiFi to actually POST readings, you have two options:

**Option A — loopback + USB tether (simplest, works without any LAN changes).**
Leave the Oracle on 127.0.0.1. The Tree never POSTs over WiFi; we'll come back to this in the troubleshooting section if it matters to you.

**Option B — Oracle on LAN (the normal v1 path).**
1. Find your PC's LAN IP: `ipconfig` and look for `IPv4 Address` under your active WiFi/Ethernet adapter, e.g. `192.168.1.42`.
2. Stop the Oracle (Ctrl+C).
3. Edit `oracle\.env`, change `ORCHARD_ORACLE_HOST=127.0.0.1` to `ORCHARD_ORACLE_HOST=0.0.0.0`. Save.
4. Add a Windows Firewall rule (PowerShell as Administrator):

   ```powershell
   New-NetFirewallRule -DisplayName "Orchard Oracle 8000" `
     -Direction Inbound -Action Allow `
     -Protocol TCP -LocalPort 8000 -Profile Private
   ```

5. Restart the Oracle.
6. Edit `dashboard\.env` (copy from `.env.example` if you haven't): set `ORCHARD_VIEW_TREE_ORACLE_URL=http://192.168.1.42:8000/readings` (use *your* LAN IP).

> **Security note:** `0.0.0.0` makes the Oracle reachable by every device on your LAN. There's no auth on `POST /register`, so don't do this on a network where you don't trust every device. See [docs/LOG.md](LOG.md) for the audit history and what to harden before exposing to the open internet.

---

## 9. Run Orchard View

In a *third* PowerShell window:

```powershell
cd C:\path\to\the-orchard
.venv\Scripts\Activate.ps1

# Copy the example env (you may have already done this in step 8)
copy dashboard\.env.example dashboard\.env

# Start the dashboard (defaults to 127.0.0.1:5000)
python -m dashboard.app
```

Open `http://127.0.0.1:5000/` in your browser. You should see the Orchard View hero banner and an "Oracle: Connected" line. The Trees list will be empty.

**Leave this window running too.** You now have three terminals open: serial monitor (optional), Oracle, Dashboard.

---

## 10. Plant your Tree

You'll need a **Chia wallet that holds at least one Orchard Pass NFT** and supports CHIP-22 WalletConnect. **Sage** and **Goby** are confirmed working; the official Chia reference wallet is on the roadmap. Don't have a Pass yet? Acquire one on [MintGarden](https://mintgarden.io/collections/col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29) and come back.

In the dashboard browser tab:

### 10a. Connect your wallet

1. Click **Connect Wallet** in the top-right nav. The official WalletConnect modal opens.
2. Pick **Sage** (or scan the QR with your mobile wallet). Approve the session in the wallet — the requested permissions are *get address* and *sign message by address*. No spend authority is requested. Ever.
3. Your wallet pops a sign prompt showing the literal text:

   ```
   Sign in to Orchard View.

   Challenge nonce: <random 64-hex string>
   If you didn't initiate this, decline.
   ```

   Approve. Behind the scenes, the wallet wraps that text in the CHIP-22 cons cell `("Chia Signed Message" . message_bytes)` and BLS-signs the tree hash. The oracle verifies both that the signature is valid AND that the pubkey derives to your claimed `xch1…` address — without the second check, an attacker could submit their own signature under someone else's address and pass.

4. The nav flips to **Connected as xch1kdzq…e3j04** with a Disconnect button. You now have a session token (HS256 JWT, 1-hour TTL by default — set `ORCHARD_ORACLE_SESSION_TTL_SECONDS` in `oracle/.env` to change).

### 10b. Plant the Tree

5. Click **Plant a Tree** in the top nav.
6. **Pick the COM port** your Tree is on. Click **Refresh ports** if it isn't listed.
7. Click **Identify Tree**. The page should show:
   - `node_id` — 32 hex characters, unique to your board.
   - `signing_key` — first 16 hex characters of the device's HMAC signing secret.
   - `fw` — the firmware version (`0.3.0` or newer; `0.4.0+` enables the next two fields).
   - `board` — Phase 9.0+: the board hint baked into the firmware build. `wroom32u` for the classic WROOM-32U enclosure, `freenove-s3` for the Freenove S3-DevKitC, or other variants in later Phase 9.x phases. Only shown on firmware 0.4.0+.
   - `chip` — Phase 9.0+: the actual ESP chip family reported at runtime (`ESP32-S3`, `ESP32-D0WD-V3`, etc.). Should agree with the `board` value above; a mismatch means the firmware was built for the wrong target. Only shown on firmware 0.4.0+.
   - `wifi`, `oracle url` — both should currently be unset.
   - `sensors` — Phase 9.0+: one chip per sensor compiled into the firmware. **Green ✓** means the sensor was probed and responded on its declared bus (BME280 ACK at 0x76/0x77, DS18B20 ROM enumerable, GPS UART seeing bytes). **Grey ⊘** means the sensor is supported but didn't respond — usually a wiring issue. Read this as an instant sanity-check that your wiring is correct before you bother provisioning. Only shown on firmware 0.4.0+; older firmware just shows the legacy fields above.
8. **Verify your Orchard Pass.** Step 2 reads your connected wallet address from the session (no typing). Click **Verify Pass**. The dashboard queries the MintGarden indexer; if your wallet holds a Pass you'll see a green confirmation showing the bound NFT (e.g. *Orchard Pass #0001*) with a link to view it on MintGarden.
   - If the dashboard says "Connect your wallet first" here, your session expired or you connected in a different tab — repeat step 10a in this tab.
   - If you don't own a Pass yet, the verification fails and the wizard halts. Acquire one on MintGarden and re-run Verify Pass.
9. Fill in the configuration step:
   - **Label** — a friendly name (optional). E.g. `backyard-1`.
   - **WiFi SSID** — your home WiFi name.
   - **WiFi password** — your home WiFi password.
   - **Oracle URL** — what the Tree should POST to. If you went with **Option B** in step 8, this should be `http://192.168.1.42:8000/readings` (your PC's LAN IP). If Option A, the Tree won't be able to reach the Oracle and we'll cover the workaround in troubleshooting.
10. Click **Provision Tree**. The browser sends the registration request with `Authorization: Bearer <your_session_token>`; the oracle uses your session's verified address as the source of truth for the Tree's wallet binding. If you ever needed to script this against the oracle directly with `curl`, the same Bearer token must be attached or `/register` returns 401 (set `ORCHARD_ORACLE_REQUIRE_WALLET_SESSION=false` in `oracle/.env` only for legacy scripts during transition).

The wizard runs four steps, each showing live status. If you bound a Pass, the first step shows the bound NFT id alongside:

```
✓ Register with oracle      — Pass bound: nft1n00ugdl737xc6ht4y…
✓ Push WiFi credentials     —
✓ Push oracle URL           —
✓ Trigger first sample      —
✓ Done — opening live view…
```

You're now redirected to `/tree/<your-node-id>`. If you bound a Pass, that page now shows an **Operator credentials** card with a link back to the NFT on MintGarden.

---

## 11. Verify the first reading

The live view polls every 5 seconds. Within **~30 seconds** of provisioning you should see:

- **Status dot turns green** with the label "Alive".
- **Last reading: just now**.
- **MQ-135 card** populated: `adc_raw`, `voltage_v`, `baseline`, `deviation`. Numbers will fluctuate — the sensor needs ~24 hours of warm-up time for stable readings, but it's working as long as the values aren't 0.
- **GPS card** populated. Outdoors with a clear view of sky: `fix: yes`, `satellites: 4` or more, `lat`/`lon` populated. Indoors near a window: `fix: yes` if you're patient (cold start can take 30+ seconds), `satellites: 0-3` if you're not.
- **Recent readings** table grows by one row per minute.

**🎉 You've planted a Tree.** The data flowing into this dashboard is signed by your device's HMAC key, verified by the Oracle on receipt, and persisted in `oracle/data/orchard.db`.

### What this Tree is doing right now

Every 60 seconds it:

1. Reads all active sensors.
2. Packages the readings into a small JSON document.
3. Computes an HMAC-SHA256 over the JSON using its on-device signing secret.
4. POSTs to your Oracle's `/readings` endpoint with the signature in `X-Orchard-Sig`.
5. The Oracle verifies the signature against the secret it learned during registration. Valid signatures get stored; invalid ones are dropped with HTTP 401.

The Tree will keep doing this forever, with no further help from you, as long as WiFi and power are available.

---

## 12. Troubleshooting

### `pio run -t upload` hangs at `Connecting....`

**Cause:** Boot-mode auto-reset isn't working on your specific board.
**Fix:** Hold BOOT, tap RESET, release BOOT after `Connecting....` succeeds. See [section 6](#6-flash-the-firmware) for full procedure.

### `Get-PnpDevice` shows no Silicon Labs entry after plugging in

**Cause:** CP210x driver not installed (Windows).
**Fix:** Install from <https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers>. Reboot if Device Manager still shows the board as "Unknown Device" after install.

### Serial monitor shows `[wifi] failed to connect: status=2`

**Cause:** Wrong SSID or password, or no WiFi/BT antenna on a WROOM-32U.
**Fix:**
- Triple-check SSID (case-sensitive) and password.
- WROOM-32U: confirm the U.FL antenna is clicked in. Without it, WiFi range is ~1 m.
- Try a 2.4 GHz WiFi (ESP32 doesn't do 5 GHz). If your router is dual-band, ensure the 2.4 GHz network is broadcasting.

### `GPS_RAW` is silent (no NMEA sentences)

**Cause:** Most common: TX/RX swapped. The GPS module's TX pin must go to the ESP32's RX pin.
**Fix:**
- WROOM-32U: GPS TX → ESP32 GPIO 18; GPS RX → ESP32 GPIO 19.
- ESP32-S3: GPS TX → GPIO 4; GPS RX → GPIO 5.
- Check power: many NEO-6M modules have an LED that blinks once per second once they have a fix. If the LED is dark, the module isn't powered.

### GPS shows `satellites: 0` but `GPS_RAW` shows valid NMEA

**Cause:** No sky view. NEO modules need direct line-of-sight to satellites for a cold start.
**Fix:** Move the GPS antenna near a window or take the Tree outside for first fix. Once it has fix-once, it remembers and re-acquires faster.

### Dashboard's "Plant a Tree" page shows no COM ports

**Cause:** Either no Tree is plugged in, or the dashboard process can't access the port (Windows file lock from another process holding the COM open).
**Fix:**
- Close any open serial monitor (`pio device monitor`).
- Click "Refresh ports".
- If still empty: check Device Manager → Ports (COM & LPT) and confirm a CP210x entry exists.

### `POST /readings` returns 401 from the Oracle (look in Oracle terminal)

**Cause:** Signature mismatch. The Tree's HMAC secret and what the Oracle has registered are different.
**Fix:** This usually means the Tree was reflashed (which doesn't regenerate the secret — it's persisted in NVS) OR the NVS was wiped. Run the provisioning wizard again from the dashboard; it'll re-register the current secret.

### Dashboard shows "Oracle unreachable"

**Cause:** The Oracle isn't running, OR `ORCHARD_VIEW_ORACLE_URL` in `dashboard\.env` points somewhere else.
**Fix:**
- Confirm the Oracle terminal still shows `Uvicorn running on http://127.0.0.1:8000`.
- Check that `dashboard\.env` has `ORCHARD_VIEW_ORACLE_URL=http://127.0.0.1:8000` (or wherever your Oracle actually is).

### Tree boots but the Oracle never sees `POST /readings`

**Cause:** Oracle bound to 127.0.0.1 and Tree is on WiFi → Tree can't reach Oracle over the LAN.
**Fix:** Either follow **Option B** in [section 8](#8-run-the-oracle), or use a different network path:
- Make the Oracle reachable from the LAN (the Option B recipe).
- Verify the Oracle URL printed in `STATUS` over serial matches what your LAN can actually reach.

### Windows Firewall keeps blocking port 8000 even after I added the rule

**Cause:** The rule was added on the wrong profile (Public vs Private). Windows treats unrecognized networks as Public.
**Fix:** Add a Public-profile rule too:

```powershell
New-NetFirewallRule -DisplayName "Orchard Oracle 8000 (Public)" `
  -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8000 -Profile Public
```

Or change the WiFi network to Private in Settings → Network & Internet → Properties → Network profile type.

### MQ-135 readings are flat or zero

**Cause:** Either the sensor isn't burned in (it needs 24-48 hours of continuous power to stabilize) or AO is wired to the wrong pin.
**Fix:**
- Confirm AO is on GPIO 34.
- Run `STATUS` over serial: if you see `mq135 bus=2 active=yes`, the driver loaded. Numbers fluctuating in the 1000-3000 range is normal during the first day.
- Power for 24+ hours before declaring it broken.

### Where to find logs

| Component | Log location |
|---|---|
| Tree firmware | Serial monitor: `pio device monitor --baud 115200 --port <COM>` |
| Oracle | The PowerShell window running `python -m oracle.app.main`. Increase verbosity with `ORCHARD_ORACLE_LOG_LEVEL=debug` in `oracle\.env`. |
| Dashboard | The PowerShell window running `python -m dashboard.app` |
| Stored readings | `oracle/data/orchard.db` (SQLite — open with DB Browser for SQLite if curious) |

---

## 13. What's next

You now have a working Tree, Oracle, and Dashboard on your machine. The next things to consider:

### Add more sensors

Drivers shipped: `mq135`, `gps_neo`, `bme280`. Drop new wires onto the breadboard, reboot the Tree, and the auto-detection should light them up. To **add a new sensor type**, see [`examples/sensor_driver/template`](../examples/sensor_driver/) — drop in a `.cpp/.h` pair, the static `AutoRegister<>` line pulls it into the sampling loop.

### Deploy outdoors

The reference enclosure is a clear ABS waterproof box with a cable gland for the GPS antenna. Mount it where:
- It has sky view for GPS.
- It has WiFi signal (-70 dBm or better).
- It's protected from direct rain (clear ABS isn't UV-stable forever).
- It's accessible for occasional troubleshooting.

### Add a Chia node and DataLayer (Phase 5)

If you want to participate in the on-chain attestation layer (writing your Tree's verified uptime to Chia DataLayer every Season), see `orchard_chia/README.md`. This requires running a Chia full node and DataLayer locally — heavier than the Tree/Oracle/Dashboard trio.

### Public network — not built yet

The path from "I planted a Tree on my machine" to "I joined a public network and earn $JUICE for verified uptime" requires:

- A **public hosted Oracle** (Phase 9).
- An **NFT-gated registration flow** that proves you own an Orchard Pass (Phase 10 / `OPERATOR_NETWORK.md`).
- Automated **$JUICE payout** to operator wallets (Phase 7 code exists; the operator role isn't decentralized yet).

Track progress in [`docs/LOG.md`](LOG.md) and the [Status & roadmap](../README.md#status--roadmap) section of the main README. The public sale of Orchard Pass NFTs will be announced separately.

### Help us improve this doc

If you got stuck on a step, that's information we want. Open an issue with what you tried, what you expected, and what happened. The `docs/LOG.md` file in this repo is our journal of every bring-up bug we've hit — yours might already be in there.

---

## Summary

| Step | Action | Time |
|---|---|---|
| 1-3 | Buy parts, identify your board | depends on shipping |
| 4 | Wire MQ-135 + GPS + antenna | 20 min |
| 5 | Install Python, PlatformIO, CP210x driver, clone repo | 30 min |
| 6 | `pio run -t upload` | 5 min |
| 7 | Watch the serial monitor for first-boot confirmations | 2 min |
| 8 | `python -m oracle.app.main` | 1 min |
| 9 | `python -m dashboard.app` | 1 min |
| 10 | Click Plant a Tree → fill in form → Provision | 3 min |
| 11 | Wait for first reading on `/tree/<id>` | ~30 sec |

Total bring-up after parts arrive: ~60 minutes for someone comfortable with a terminal.

Welcome to The Orchard.
