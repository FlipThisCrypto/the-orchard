# SPDX-License-Identifier: Apache-2.0
"""USB-serial talk to a Tree's provisioning console.

Each helper opens a fresh serial connection, sends a single command,
reads the line-oriented `OK ...` / `ERR ...` reply, and closes. No
persistent connection — keeps Flask request handling stateless.

Mirrors the command set in firmware/src/net/serial_console.{h,cpp}.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports

from .config import settings

DEFAULT_BAUD = 115200

# Control characters (especially newline/CR) must never reach a value
# that gets interpolated into a line-oriented serial command — the
# device console dispatches one command per newline, so a value like
# "pw\nORACLE_SET http://evil" would inject a SECOND firmware command.
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Allowed shape for an oracle URL pushed to a Tree (no spaces/control chars).
_HTTP_URL = re.compile(r"^https?://[^\s]+$")


@dataclass
class PortInfo:
    device: str
    description: str
    hwid: str


class TreeError(RuntimeError):
    """Anything that goes wrong talking to a Tree."""


def _reject_ctrl(value: str, field: str) -> None:
    """Block serial-command injection: refuse control chars in a value
    that becomes part of a console command line."""
    if value and _CTRL_CHARS.search(value):
        raise TreeError(
            f"{field} contains control characters — newlines/control "
            f"characters are not allowed (serial-injection guard)"
        )


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


def _send_and_read_line(
    port: str,
    cmd: str,
    *,
    timeout_override: float | None = None,
) -> str:
    """Send `cmd\\n`, read lines until we see one starting with OK/ERR.

    Skips background log lines (sensor reports, wifi messages, etc.).
    If the command times out, the error message includes the last few
    non-matching lines we saw — invaluable when debugging.

    ``timeout_override`` lets known-slow commands (WIFI_SET kicks an
    asynchronous WiFi connect that the firmware ack's only after the
    blocking-ish call returns) wait longer than the default 3s without
    bumping the default for fast commands like PING / STATUS / HW_INFO.
    """
    eff_timeout = (
        timeout_override
        if timeout_override is not None
        else settings().serial_timeout
    )
    with _open(port) as s:
        # If we have a longer timeout, the port-level read timeout
        # also needs to be at least that long, otherwise readline()
        # returns empty after the per-read timeout and we'd be
        # polling the deadline in tight 3s slices.
        if eff_timeout > s.timeout:
            s.timeout = eff_timeout
        s.write((cmd + "\n").encode("utf-8"))
        s.flush()
        deadline = time.time() + eff_timeout
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
            f"no response from {port} to {cmd!r} within {eff_timeout}s. "
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
    # Serial-injection guard: a newline in the SSID or password would
    # start a second firmware command on the device console.
    _reject_ctrl(ssid, "ssid")
    _reject_ctrl(password, "password")
    if " " in ssid:
        # The simple v1 command parser splits on the first space; SSIDs
        # with spaces aren't supported until we add a quoted form.
        raise TreeError("SSID cannot contain spaces in v1")
    # WIFI_SET stores creds in NVS then kicks a reconnect via
    # WiFi.begin() — the firmware ack's `OK` only after that returns.
    # WiFi.begin() can spin ~10s on a marginal AP (auth handshake,
    # DHCP, etc.) so the default 3s timeout is too short. Give it 20s
    # of headroom; if a Tree can't make progress in 20s, the operator
    # has a network problem to fix, not a serial-comm one.
    line = _send_and_read_line(
        port, f"WIFI_SET {ssid} {password}",
        timeout_override=20.0,
    )
    if not line.startswith("OK"):
        raise TreeError(f"WIFI_SET: {line}")


def set_oracle_url(port: str, url: str) -> None:
    # Serial-injection guard + scheme allowlist: the URL is the device's
    # data sink, so a newline (extra command) or a non-http scheme must
    # be rejected before it reaches the console.
    _reject_ctrl(url, "url")
    if not _HTTP_URL.match(url or ""):
        raise TreeError("oracle url must be an http(s):// URL with no spaces")
    line = _send_and_read_line(port, f"ORACLE_SET {url}")
    if not line.startswith("OK"):
        raise TreeError(f"ORACLE_SET: {line}")


def sample_now(port: str) -> None:
    # SAMPLE_NOW samples every active sensor AND POSTs the resulting
    # JSON to the oracle. The POST is an HTTPClient call that can take
    # 500ms-3s on a marginal WiFi link or busy oracle. Give it 10s of
    # headroom — longer than the wizard's 3s default but still bounded
    # so an unresponsive oracle doesn't hang the wizard forever.
    line = _send_and_read_line(
        port, "SAMPLE_NOW",
        timeout_override=10.0,
    )
    if not line.startswith("OK"):
        raise TreeError(f"SAMPLE_NOW: {line}")
