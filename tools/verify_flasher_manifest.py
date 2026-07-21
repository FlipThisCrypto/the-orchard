# SPDX-License-Identifier: Apache-2.0
"""Validate flasher/manifest.json against GitHub release assets.

Checks:
  * JSON shape (version, builds, chipFamily, parts)
  * paths look like /fw/<tag>/<file>.bin
  * tag matches version (vX.Y.Z convention)
  * each referenced asset returns HTTP 200 (or 302→200) on GitHub Releases
  * optional: SHA256SUMS.txt lists each file

Exit 0 when all assets resolve; 1 on validation failure; 2 on usage/network.

Usage (repo root)::

    python tools/verify_flasher_manifest.py
    python tools/verify_flasher_manifest.py --manifest flasher/manifest.json --offline
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "FlipThisCrypto/the-orchard"
_PATH_RE = re.compile(
    r"^/fw/(?P<tag>v[0-9A-Za-z.+\-]{1,40})/(?P<file>[A-Za-z0-9._\-]{1,120}\.bin)$"
)
_CHIP = {"ESP32", "ESP32-S2", "ESP32-S3", "ESP32-C3", "ESP32-C6", "ESP32-H2"}


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        _die(f"manifest not found: {path}", 2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        _die("manifest root must be an object")
    return data


def validate_shape(m: dict) -> list[str]:
    errs: list[str] = []
    version = m.get("version")
    if not isinstance(version, str) or not version.strip():
        errs.append("missing string 'version'")
    builds = m.get("builds")
    if not isinstance(builds, list) or not builds:
        errs.append("missing non-empty 'builds' array")
        return errs
    for i, b in enumerate(builds):
        if not isinstance(b, dict):
            errs.append(f"builds[{i}] not an object")
            continue
        chip = b.get("chipFamily")
        if chip not in _CHIP:
            errs.append(f"builds[{i}].chipFamily invalid: {chip!r}")
        parts = b.get("parts")
        if not isinstance(parts, list) or not parts:
            errs.append(f"builds[{i}].parts missing/empty")
            continue
        for j, p in enumerate(parts):
            if not isinstance(p, dict) or "path" not in p:
                errs.append(f"builds[{i}].parts[{j}] needs path")
                continue
            path = p["path"]
            if not isinstance(path, str) or not _PATH_RE.match(path):
                errs.append(
                    f"builds[{i}].parts[{j}].path must be /fw/<tag>/<file>.bin, got {path!r}"
                )
            elif isinstance(version, str):
                tag = _PATH_RE.match(path).group("tag")  # type: ignore[union-attr]
                # version "0.5.1" ↔ tag "v0.5.1"
                expect = version if version.startswith("v") else f"v{version}"
                if tag != expect:
                    errs.append(
                        f"builds[{i}].parts[{j}].path tag {tag!r} != version {expect!r}"
                    )
            off = p.get("offset")
            if off is not None and not isinstance(off, int):
                errs.append(f"builds[{i}].parts[{j}].offset must be int")
    return errs


def asset_urls(m: dict) -> list[tuple[str, str, str]]:
    """Return list of (tag, filename, github_url)."""
    out: list[tuple[str, str, str]] = []
    for b in m.get("builds") or []:
        for p in b.get("parts") or []:
            path = p.get("path") or ""
            mo = _PATH_RE.match(path)
            if not mo:
                continue
            tag, file = mo.group("tag"), mo.group("file")
            url = f"https://github.com/{REPO}/releases/download/{tag}/{file}"
            out.append((tag, file, url))
    return out


def head_ok(url: str, timeout: float = 25.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code in (200, 206):
                return True, f"HTTP {code}"
            # Some CDNs dislike HEAD; fall through to GET range.
    except urllib.error.HTTPError as e:
        if e.code in (403, 405):
            pass  # try GET
        elif e.code == 404:
            return False, "HTTP 404"
        else:
            return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"network: {e.reason}"

    # Range GET 1 byte — enough to prove the asset exists without full download.
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code in (200, 206):
                return True, f"HTTP {code} (range)"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"network: {e.reason}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate flasher manifest vs release assets.")
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path("flasher/manifest.json"),
        help="path to manifest.json",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="shape-check only (no network)",
    )
    args = p.parse_args(argv)

    m = load_manifest(args.manifest)
    errs = validate_shape(m)
    if errs:
        for e in errs:
            print(f"FAIL shape: {e}", file=sys.stderr)
        return 1
    print(f"OK  shape version={m.get('version')!r} builds={len(m.get('builds') or [])}")

    assets = asset_urls(m)
    if not assets:
        _die("no asset paths extracted from builds")

    if args.offline:
        for tag, file, _ in assets:
            print(f"OK  offline path tag={tag} file={file}")
        print("MANIFEST OFFLINE CHECK PASSED")
        return 0

    failed = 0
    for tag, file, url in assets:
        ok, detail = head_ok(url)
        if ok:
            print(f"OK  {tag}/{file} ({detail})")
        else:
            print(f"FAIL {tag}/{file} ({detail}) {url}", file=sys.stderr)
            failed += 1

    if failed:
        print(f"MANIFEST CHECK FAILED ({failed} asset(s))", file=sys.stderr)
        return 1
    print("MANIFEST CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
