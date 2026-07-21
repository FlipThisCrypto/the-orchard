# SPDX-License-Identifier: Apache-2.0
"""Tests for tools.verify_flasher_manifest (offline shape checks)."""
from __future__ import annotations

import json
from pathlib import Path

from tools.verify_flasher_manifest import main, validate_shape


def test_repo_manifest_shape_offline():
    assert main(["--offline", "--manifest", "flasher/manifest.json"]) == 0


def test_bad_path_fails(tmp_path: Path):
    m = {
        "version": "0.5.1",
        "builds": [
            {
                "chipFamily": "ESP32",
                "parts": [{"path": "https://evil.example/x.bin", "offset": 0}],
            }
        ],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    assert main(["--offline", "--manifest", str(p)]) == 1


def test_tag_version_mismatch(tmp_path: Path):
    m = {
        "version": "0.5.1",
        "builds": [
            {
                "chipFamily": "ESP32",
                "parts": [
                    {
                        "path": "/fw/v0.4.0/orchard-freenove_esp32_wroom-web-v0.4.0.bin",
                        "offset": 0,
                    }
                ],
            }
        ],
    }
    errs = validate_shape(m)
    assert any("tag" in e and "version" in e for e in errs)
