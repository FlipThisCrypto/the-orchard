# Contributing to The Orchard

Thanks for your interest. The Orchard is meant to be a community-buildable DePIN — forks, sensor-driver additions, hardware variants, and bug fixes are all welcome.

This document covers: how to set up a dev environment, the code style we follow, how to propose changes, and how the contribution lifecycle works.

---

## Ground rules

- **Be kind.** This is a small project run by people who have day jobs. Assume good intent.
- **Don't commit secrets.** No wallet mnemonics, private keys, `.env` files, or API tokens. The [.gitignore](.gitignore) is set up to catch the common cases — but verify before pushing.
- **Document what you do.** If your change has a non-obvious "why," add a note in [docs/LOG.md](docs/LOG.md) or a new ADR in [docs/decisions/](docs/decisions/). Future contributors (including future-you) will thank you.

---

## Setting up a dev environment

### Prerequisites

- **Python 3.11+** for the oracle and dashboard.
- **PlatformIO** (via VS Code extension or `pip install platformio`) for the ESP32 firmware.
- **Chia full node + DataLayer service** for end-to-end work (not required for firmware-only contributions).
- **A Freenove ESP32-S3** (or compatible ESP32-S3 board) — strongly recommended for testing, since pin mappings differ from ESP32-WROOM.

### Get the code

```bash
git clone https://github.com/FlipThisCrypto/the-orchard.git
cd the-orchard
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate
pip install -r oracle/requirements.txt -r dashboard/requirements.txt
```

Firmware:

```bash
cd firmware
pio run            # build
pio run -t upload  # flash over USB
pio device monitor # serial console
```

---

## Adding a new sensor driver

This is the most common contribution we expect. The firmware has a **self-registering sensor driver** interface. To add one:

1. Drop a new `.cpp/.h` pair in [firmware/src/sensors/](firmware/src/sensors/). Follow the template in [examples/sensor_driver/](examples/sensor_driver/).
2. Implement: `begin()`, `read(JsonObject &out)`, `bus_type` (I2C, UART, ANALOG, DIGITAL), and either an I2C address or a UART signature for auto-detection where possible.
3. Add a wiring doc in [docs/wiring/](docs/wiring/).
4. (Optional but appreciated) Add a unit test or a hardware-in-the-loop bring-up note in `docs/LOG.md`.

---

## Code style

- **Python:** PEP 8. We'll add `ruff` config in Phase 3.
- **C++ (firmware):** Roughly Google C++ style, but small files and clear names matter more than rigid formatting.
- **Markdown:** Reference-style links for repeated URLs, fenced code blocks with language hints.

---

## Proposing changes

1. Open an issue first for non-trivial changes — it's cheaper to discuss the approach in 10 sentences than to re-do 500 lines.
2. Branch off `main`. Branch names: `feature/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
3. Commit in small logical steps. Commit messages: imperative present tense ("Add BH1750 driver", not "Added BH1750 driver").
4. Open a PR against `main` with: what changed, why, how it was tested.
5. Update [docs/LOG.md](docs/LOG.md) with what worked / didn't if relevant.

---

## What we'd love help with

- Sensor driver ports (especially I2C combo modules: BME280, SHT41, SGP40).
- Wiring diagrams as images, not just tables.
- A non-Windows tested install path (we develop on Windows; macOS/Linux testers welcome).
- Chialisp expertise for the eventual on-chain claim contract (Phase 8+).
- Translations of the dashboard UI.

---

## Code of Conduct

Be respectful. Disagreements about technical decisions are fine; personal attacks are not. Maintainers reserve the right to close issues or PRs that don't follow this.

---

## License

By contributing you agree your contributions are licensed under the [Apache License 2.0](LICENSE), same as the rest of the project.

## DataLayer

Run `python -m pytest orchard_chia/tests -q` after changing `orchard_chia/datalayer`. See `docs/ops/DATALAYER_OPERATOR.md`.

