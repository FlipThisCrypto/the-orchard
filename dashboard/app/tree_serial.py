# SPDX-License-Identifier: Apache-2.0
"""USB-serial talk to a Tree's provisioning console.

Each helper opens a fresh serial connection, sends a single command,
reads the line-oriented `OK ...` / `ERR ...` reply, and closes. No
persistent connection — keeps Flask request handling stateless.

Mirrors the command set in firmware/src/net/serial_console.{h,cpp}.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports

from .config import settings

DEFAULT_BAUD = 115200


@dataclass
class PortInfo:
    device: str
    description: str
    hwid: str


class TreeError(RuntimeError):
    """Anything that goes wrong talking to a Tree."""


def list_ports() -> list[PortInfo]:
    """Enumerate every COM port pyserial can see."""
    return [
        PortInfo(p.device, p.description or "", p.hwid or "")
        for p in serial.tools.list_ports.comports()
    ]


def _open(port: str) -> serial.Serial:
    """Open the port WITHOUT asserting DTR/RTS, so we don't accidentally
    reset the ESP32 via the USB-UART bridge's auto-reset circuit
    (the CP210x / CH34x typically wire DTR -> IO0 and RTS -> EN through
    a couple of transistors). Set the lines before opening so the
    transition during `open()` is into the de-asserted state, not a pulse.
    """
    try:
        s = serial.Serial()
        s.port = port
        s.baudrate = DEFAULT_BAUD
        s.timeout = settings().serial_timeout
        s.write_timeout = settings().serial_timeout
        s.dtr = False
        s.rts = False
        s.open()
    except serial.SerialException as e:
        raise TreeError(f"could not open {port}: {e}") from e
    # Brief settle + drain so any in-flight log line is consumed before
    # we transmit a command.
    time.sleep(0.12)
    s.reset_input_buffer()
    return s


def _send_and_read_line(port: str, cmd: str) -> str:
    """Send `cmd\\n`, read lines until we see one starting with OK/ERR.

    Skips background log lines (sensor reports, wifi messages, etc.).
    If the command times out, the error message includes the last few
    non-matching lines we saw — invaluable when debugging.
    """
    with _open(port) as s:
        s.write((cmd + "\n").encode("utf-8"))
        s.flush()
        deadline = time.time() + settings().serial_timeout
        seen: list[str] = []
        while time.time() < deadline:
            line = s.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # A valid response is `OK`, `OK <rest>`, or `ERR <rest>`.
            if line == "OK" or line.startswith("OK ") or line.startswith("ERR"):
                return line
            seen.append(line)
            if len(seen) > 8:
                seen = seen[-8:]
        excerpt = " | ".join(seen) if seen else "<no output at all>"
        raise TreeError(
            f"no response from {port} to {cmd!r} within {settings().serial_timeout}s. "
            f"Recent serial output: {excerpt}"
        )


def ping(port: str) -> bool:
    line = _send_and_read_line(port, "PING")
    return line.startswith("OK")


def get_node_id(port: str) -> str:
    line = _send_and_read_line(port, "NODE_ID")
    if not line.startswith("OK "):
        raise TreeError(f"NODE_ID: {line}")
    return line[3:].strip()


def get_signing_key(port: str) -> str:
    line = _send_and_read_line(port, "KEY")
    if not line.startswith("OK "):
        raise TreeError(f"KEY: {line}")
    return line[3:].strip()


def get_status(port: str) -> dict:
    line = _send_and_read_line(port, "STATUS")
    if not line.startswith("OK "):
        raise TreeError(f"STATUS: {line}")
    payload = line[3:].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise TreeError(f"STATUS payload not JSON: {payload!r} ({e})") from e


def get_hw_info(port: str) -> dict | None:
    """Phase 9.0 — board + sensor fingerprint, or None for legacy fw.

    Older firmware (<0.4.0) responds with ``ERR unknown`` to HW_INFO;
    we map that to None rather than raising, so the wizard can fall
    back to the pre-9.0 identify card without erroring out. Any OTHER
    failure (timeout, malformed JSON, etc.) still raises, because
    those are real problems the operator should see.

    Returned dict shape mirrors firmware/src/net/serial_console.cpp
    cmd_hw_info_:
        {
          "fw":      "0.4.0",
          "chip":    "ESP32-S3" | "ESP32-D0WD-V3" | ...,
          "board":   "wroom32u" | "freenove-s3" | "generic" | ...,
          "node_id": "<32 hex>",
          "sensors": [
            {"name": "bme280", "bus": "i2c",    "addr": 118, "active": true},
            {"name": "mq135",  "bus": "analog",              "active": true},
            ...
          ],
        }
    """
    line = _send_and_read_line(port, "HW_INFO")
    if line.startswith("ERR unknown"):
        return None
    if not line.startswith("OK "):
        raise TreeError(f"HW_INFO: {line}")
    payload = line[3:].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise TreeError(f"HW_INFO payload not JSON: {payload!r} ({e})") from e


def set_wifi(port: str, ssid: str, password: str) -> None:
    if " " in ssid:
        # The simple v1 command parser splits on the first space; SSIDs
        # with spaces aren't supported until we add a quoted form.
        raise TreeError("SSID cannot contain spaces in v1")
    line = _send_and_read_line(port, f"WIFI_SET {ssid} {password}")
    if not line.startswith("OK"):
        raise TreeError(f"WIFI_SET: {line}")


def set_oracle_url(port: str, url: str) -> None:
    line = _send_and_read_line(port, f"ORACLE_SET {url}")
    if not line.startswith("OK"):
        raise TreeError(f"ORACLE_SET: {line}")


def sample_now(port: str) -> None:
    line = _send_and_read_line(port, "SAMPLE_NOW")
    if not line.startswith("OK"):
        raise TreeError(f"SAMPLE_NOW: {line}")
