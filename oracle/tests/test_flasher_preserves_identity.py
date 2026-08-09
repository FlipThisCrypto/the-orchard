# SPDX-License-Identifier: Apache-2.0
"""Re-flashing a Tree must upgrade it, not replace it.

The web installer shipped with ``new_install_prompt_erase: true``. Erasing wipes
NVS, which is where a Tree's node_id, secp256r1 provenance key, HMAC secret and
Pass claim nonce live. firmware/src/identity.cpp then boots, finds nothing, and
does exactly what it is written to do — mints a fresh identity and logs
"generated new node id".

So every re-flash created a NEW Tree and orphaned the old one, which then sat in
the oracle showing "not seen in N days" forever. The evidence on the live
network: SIX registered node_ids sharing ONE Orchard Pass NFT, three of them
registered within six minutes of each other on 2026-06-16 (09:05, 09:09, 09:11).
Nobody deploys three sensor Trees to three locations in six minutes. It was one
board, reflashed.

This also inflated the network's apparent size — a stranger reading the chain
saw six independent Trees where there were one or two boards on one desk.

An erase is not needed for an update: partition layouts are stable per board
(ESP32 uses Arduino-ESP32's built-in default.csv, S3 uses a fixed
partitions.csv) and the manifest writes a merged image at offset 0.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "flasher" / "manifest.json"
PAGE = REPO / "flasher" / "index.html"
IDENTITY = REPO / "firmware" / "src" / "identity.cpp"


def test_the_installer_does_not_erase():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert m["new_install_prompt_erase"] is False, (
        "erasing wipes NVS and mints a new node_id — every re-flash would "
        "orphan the Tree it was meant to upgrade"
    )


def test_the_manifest_explains_why_to_whoever_opens_it_next():
    """A bare `false` invites a well-meaning revert."""
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    note = m.get("_note_new_install_prompt_erase", "")
    assert "NVS" in note and "node_id" in note


def test_the_page_no_longer_promises_an_erase():
    html = PAGE.read_text(encoding="utf-8")
    assert "installer erases the board" not in html, (
        "the page described the old behaviour; copy and behaviour must agree"
    )
    assert "keeps its identity" in html


def test_the_firmware_still_generates_an_identity_when_there_genuinely_is_none():
    """The fix is at the installer, not the firmware — a truly blank board must
    still self-provision on first boot."""
    src = IDENTITY.read_text(encoding="utf-8")
    assert "if (need_node) {" in src
    assert "generated new node id" in src


def test_identity_is_read_before_it_is_generated():
    """Read-then-generate is what makes a non-erasing flash preserve identity.
    If generation ever moved ahead of the read, every boot would be a new Tree."""
    src = IDENTITY.read_text(encoding="utf-8")
    read_at = src.index("prefs.getBytes(kNvsKeyNodeId")
    gen_at = src.index("random_bytes(node_id_buf")
    assert read_at < gen_at, "NVS must be consulted before any key is minted"


def test_every_identity_field_is_persisted():
    """All four must survive, not just node_id: losing the P-256 key alone would
    leave a Tree that reports but can never be published, because the oracle
    refuses to rotate a published provenance key."""
    src = IDENTITY.read_text(encoding="utf-8")
    for key in ("kNvsKeyNodeId", "kNvsKeySecret", "kNvsKeyP256", "kNvsKeyClaimNonce"):
        assert f"prefs.putBytes({key}" in src, f"{key} is not persisted"
