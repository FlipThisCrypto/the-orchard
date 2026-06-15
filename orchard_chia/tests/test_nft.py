# SPDX-License-Identifier: Apache-2.0
"""Tests for the pure functions in orchard_chia.nft.

Hermetic — no wallet, no network. Tests the metadata generator,
canonicalization, hashing, and mint-plan validation logic.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchard_chia.nft import generate, mint, verify


# ---------------- generate ----------------

def test_collection_metadata_shape():
    c = generate.build_collection_metadata()
    assert c["id"] == generate.ORCHARD_GENESIS_COLLECTION_ID
    assert c["name"] == generate.ORCHARD_GENESIS_COLLECTION_NAME
    assert any(a["type"] == "description" for a in c["attributes"])


def test_pass_metadata_basic():
    p = generate.build_pass_metadata(pass_number=1)
    assert p["format"] == "CHIP-0007"
    assert p["name"] == "Orchard Pass #0001"
    assert p["minting_tool"] == generate.MINTING_TOOL
    # series_* and sensitive_content are intentionally omitted to match
    # the mintgarden-studio reference shape.
    assert "series_number" not in p
    assert "series_total" not in p
    assert "sensitive_content" not in p
    # Collection reference matches the collection metadata.
    assert p["collection"]["id"] == generate.ORCHARD_GENESIS_COLLECTION_ID
    # Standard genesis attributes are present.
    traits = {a["trait_type"]: a["value"] for a in p["attributes"]}
    assert traits["Generation"] == "Genesis"
    assert traits["Pass Number"] == "0001"
    assert traits["Reward Token"] == "$JUICE"


def test_pass_metadata_pass_number_bounds():
    with pytest.raises(ValueError):
        generate.build_pass_metadata(pass_number=0)
    with pytest.raises(ValueError):
        generate.build_pass_metadata(pass_number=11)
    # Edge cases at the bounds work.
    generate.build_pass_metadata(pass_number=1)
    generate.build_pass_metadata(pass_number=10)


def test_pass_metadata_extra_attributes():
    p = generate.build_pass_metadata(
        pass_number=3,
        extra_attributes=[{"trait_type": "Animator", "value": "Richard"}],
    )
    traits = {a["trait_type"]: a["value"] for a in p["attributes"]}
    assert traits["Animator"] == "Richard"


def test_canonical_json_is_deterministic():
    p = generate.build_pass_metadata(pass_number=5)
    a = generate.canonical_json(p)
    b = generate.canonical_json(p)
    assert a == b
    # Re-encoding through the same canonicalizer must produce the same
    # bytes. Note: we don't ban ", " from the *output string* because
    # value contents can legitimately contain it (e.g. descriptions);
    # the JSON *structural* separators are what matter.
    parsed = json.loads(a)
    assert generate.canonical_json(parsed) == a
    # Keys are sorted (top-level).
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_sha256_hex_matches_known_value():
    h = generate.sha256_hex(b"hello world")
    assert h == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_write_genesis_batch(tmp_path: Path):
    written = generate.write_genesis_batch(tmp_path, total=10)
    assert len(written) == 10
    # All 10 files exist with the expected zero-padded names.
    for n in range(1, 11):
        p = tmp_path / f"{n:04d}.json"
        assert p.exists()
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert doc["name"] == f"Orchard Pass #{n:04d}"
        assert doc["collection"]["id"] == generate.ORCHARD_GENESIS_COLLECTION_ID
        assert doc["collection"]["id"] == generate.ORCHARD_GENESIS_COLLECTION_ID


# ---------------- mint plan validation ----------------

GOOD_PLAN_YAML = """
collection_id: "f9a0c0a0-0001-4000-8000-000000000001"
target_address: "xch1abcdef"
royalty_address: "xch1abcdef"
royalty_percentage: 0
edition_total: 2
fee_mojos: 0
passes:
  - edition_number: 1
    metadata_file: "metadata/0001.json"
    data_uris: ["ipfs://bafy.../001.mp4"]
    data_hash: "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    meta_uris: ["ipfs://bafy.../0001.json"]
    meta_hash: "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"
  - edition_number: 2
    metadata_file: "metadata/0002.json"
    data_uris: ["ipfs://bafy.../002.mp4"]
    data_hash: "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    meta_uris: ["ipfs://bafy.../0002.json"]
    meta_hash: "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"
"""


def test_load_plan(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(GOOD_PLAN_YAML, encoding="utf-8")
    plan = mint.load_plan(p)
    assert plan.edition_total == 2
    assert len(plan.passes) == 2
    assert plan.passes[0].edition_number == 1


def test_validate_good_plan(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(GOOD_PLAN_YAML, encoding="utf-8")
    plan = mint.load_plan(p)
    # Stub the metadata files so the file-exists check passes.
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "0001.json").write_text("{}", encoding="utf-8")
    (tmp_path / "metadata" / "0002.json").write_text("{}", encoding="utf-8")
    problems = mint.validate_plan(plan, plan_path=p)
    assert problems == []


def test_validate_catches_bad_address(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    yaml_bad = GOOD_PLAN_YAML.replace('"xch1abcdef"', '"NOT_AN_XCH_ADDRESS"')
    p.write_text(yaml_bad, encoding="utf-8")
    plan = mint.load_plan(p)
    problems = mint.validate_plan(plan, plan_path=None)
    assert any("target_address" in pr for pr in problems)


def test_validate_catches_bad_hash_length(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    yaml_bad = GOOD_PLAN_YAML.replace(
        "data_hash: \"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff\"",
        "data_hash: \"deadbeef\"",
    )
    p.write_text(yaml_bad, encoding="utf-8")
    plan = mint.load_plan(p)
    problems = mint.validate_plan(plan, plan_path=None)
    assert any("data_hash" in pr for pr in problems)


def test_validate_catches_empty_uris(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    yaml_bad = GOOD_PLAN_YAML.replace(
        'data_uris: ["ipfs://bafy.../001.mp4"]',
        'data_uris: []',
    )
    p.write_text(yaml_bad, encoding="utf-8")
    plan = mint.load_plan(p)
    problems = mint.validate_plan(plan, plan_path=None)
    assert any("data_uris is empty" in pr for pr in problems)


def test_validate_catches_duplicate_edition(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    yaml_bad = GOOD_PLAN_YAML.replace("edition_number: 2", "edition_number: 1")
    p.write_text(yaml_bad, encoding="utf-8")
    plan = mint.load_plan(p)
    problems = mint.validate_plan(plan, plan_path=None)
    assert any("duplicate edition_number" in pr for pr in problems)


# ---------------- verify (indexer path) ----------------

# Trimmed copy of the real MintGarden response shape — just enough fields
# to exercise the filter + normalizer without depending on the network.
_MG_SAMPLE_ITEMS = {
    "items": [
        {
            "id": "032f683dacc4b64fd7e8615d010f010083929dacab70321837becea221a467e9",
            "encoded_id": "nft1qvhks0dvcjmyl4lgv9wszrcpqzpe98dv4dcryxphhm82ygdyvl5s2xnrd7",
            "name": "Orchard Pass #0003",
            "edition_number": 1,
            "edition_total": 1,
            "owner_address_encoded_id": "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n",
            "collection_id": generate.ORCHARD_GENESIS_COLLECTION_BECH32_ID,
        },
        {
            "id": "aaaaaaaa11111111aaaaaaaa11111111aaaaaaaa11111111aaaaaaaa11111111",
            "encoded_id": "nft1aaaaaaaa11111111aaaaaaaa11111111aaaaaaaa1111111100",
            "name": "Orchard Pass #0001",
            "edition_number": 1,
            "edition_total": 1,
            # Different owner — should be filtered out.
            "owner_address_encoded_id": "xch1someoneelse00000000000000000000000000000000000000000000000000",
            "collection_id": generate.ORCHARD_GENESIS_COLLECTION_BECH32_ID,
        },
    ],
}


def _canned_opener(_url: str) -> bytes:
    return json.dumps(_MG_SAMPLE_ITEMS).encode("utf-8")


def test_list_passes_by_address_filters_to_owner():
    addr = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
    out = verify.list_passes_by_address(addr, _opener=_canned_opener)
    assert len(out) == 1
    p = out[0]
    assert p["name"] == "Orchard Pass #0003"
    assert p["edition_number"] == 1
    assert p["owner_address"] == addr
    # Bech32 nft_coin_id surfaced for consumers that want a pretty id.
    assert p["nft_coin_id"].startswith("nft1")
    # Hex launcher id still available for the wallet RPC path.
    assert len(p["launcher_id"]) == 64
    # Tag identifies the source for downstream logging.
    assert p["_source"] == "mintgarden"


def test_list_passes_by_address_other_owner_returns_empty():
    out = verify.list_passes_by_address(
        "xch1nobodyownsthisaddress00000000000000000000000000000000000000",
        _opener=_canned_opener,
    )
    assert out == []


def test_address_holds_pass_boolean_wrapper():
    addr = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
    assert verify.address_holds_pass(addr, _opener=_canned_opener) is True
    assert verify.address_holds_pass(
        "xch1nobody00000000", _opener=_canned_opener) is False


def test_indexer_error_on_bad_json():
    def bad_opener(_url: str) -> bytes:
        return b"<html>oops</html>"
    with pytest.raises(verify.IndexerError):
        verify.list_passes_by_address("xch1anything", _opener=bad_opener)


def test_list_passes_rejects_wrong_collection_id():
    """M7: even if an item is returned with the right owner, a non-genesis
    collection_id must be excluded (defensive against indexer drift)."""
    addr = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
    fake = {"items": [{
        "id": "ab" * 32,
        "encoded_id": "nft1fake",
        "name": "Fake Pass",
        "edition_number": 1,
        "owner_address_encoded_id": addr,
        "collection_id": "col1notthegenesiscollectionatall",
    }]}
    out = verify.list_passes_by_address(
        addr, _opener=lambda _u: json.dumps(fake).encode("utf-8"))
    assert out == []


# ---- MintGarden pagination (must never silently truncate the Pass list) ----

def _page(n, cursor=None):
    body = {"items": [{"id": f"{i:064x}", "name": f"NFT{i}"} for i in range(n)]}
    if cursor is not None:
        body["next"] = cursor
    return json.dumps(body).encode("utf-8")


def test_fetch_walks_all_pages_via_cursor():
    size = verify.INDEXER_PAGE_SIZE

    def opener(url: str) -> bytes:
        if "page=" not in url:
            return _page(size, cursor="CURSOR2")      # full page + cursor
        assert "page=CURSOR2" in url
        return _page(3)                                # short final page

    items = verify._fetch_mintgarden_collection_items(_opener=opener)
    assert len(items) == size + 3


def test_full_page_without_cursor_raises_not_truncates():
    # A full page with no recognized cursor must fail loud, not drop the rest.
    def opener(_url: str) -> bytes:
        return _page(verify.INDEXER_PAGE_SIZE)  # full page, no 'next'
    with pytest.raises(verify.IndexerError):
        verify._fetch_mintgarden_collection_items(_opener=opener)


def test_single_short_page_is_complete():
    def opener(_url: str) -> bytes:
        return _page(4)  # well under page_size -> done, no error
    assert len(verify._fetch_mintgarden_collection_items(_opener=opener)) == 4
