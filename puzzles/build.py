# SPDX-License-Identifier: Apache-2.0
"""Compile the Orchard ChiaLisp puzzles and pin their hashes (HANDOVER T19).

Compiles every ``puzzles/src/*.clsp`` to serialized CLVM, and records — in
``puzzles/hashes.json`` — the compiled hex, its sha256, and the CLVM
**treehash** (the on-chain puzzle hash, sha256tree). Committing those makes a
puzzle's bytecode auditable and lets CI fail if a ``.clsp`` edit changes the
compiled output without the pin being regenerated.

Usage (from the repo root):
    python puzzles/build.py            # (re)compile + write hashes.json
    python puzzles/build.py --check    # recompile, fail if hashes.json drifts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from clvm_tools.clvmc import compile_clvm_text

PUZZLES_DIR = Path(__file__).resolve().parent
SRC_DIR = PUZZLES_DIR / "src"
HASHES_FILE = PUZZLES_DIR / "hashes.json"


def _sha256tree(sexp) -> bytes:
    """CLVM treehash (sha256tree): the on-chain puzzle hash. Atom ->
    sha256(0x01 || atom); pair -> sha256(0x02 || treehash(l) || treehash(r))."""
    pair = sexp.pair
    if pair is not None:
        return hashlib.sha256(b"\x02" + _sha256tree(pair[0]) + _sha256tree(pair[1])).digest()
    return hashlib.sha256(b"\x01" + sexp.atom).digest()


def compile_one(clsp_path: Path) -> dict:
    text = clsp_path.read_text(encoding="utf-8")
    prog = compile_clvm_text(text, search_paths=[str(SRC_DIR)])
    blob = prog.as_bin()
    return {
        "clvm_hex": blob.hex(),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "treehash": _sha256tree(prog).hex(),
    }


def build_all() -> dict:
    out: dict[str, dict] = {}
    for clsp in sorted(SRC_DIR.glob("*.clsp")):
        out[clsp.stem] = compile_one(clsp)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python puzzles/build.py")
    ap.add_argument("--check", action="store_true",
                    help="recompile and fail if hashes.json is out of date")
    args = ap.parse_args(argv)

    built = build_all()
    if not built:
        print("no .clsp sources found in puzzles/src/", file=sys.stderr)
        return 1

    if args.check:
        if not HASHES_FILE.exists():
            print("hashes.json missing — run `python puzzles/build.py`", file=sys.stderr)
            return 1
        committed = json.loads(HASHES_FILE.read_text(encoding="utf-8"))
        if committed != built:
            print("DRIFT: puzzles/hashes.json is stale vs a fresh compile. "
                  "Re-run `python puzzles/build.py` and commit.", file=sys.stderr)
            for name in sorted(set(built) | set(committed)):
                if committed.get(name) != built.get(name):
                    print(f"  changed: {name}", file=sys.stderr)
            return 1
        print(f"OK — {len(built)} puzzle(s) match hashes.json")
        return 0

    HASHES_FILE.write_text(json.dumps(built, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    for name, info in built.items():
        print(f"{name}: treehash={info['treehash'][:16]}… ({len(info['clvm_hex']) // 2} bytes)")
    print(f"wrote {HASHES_FILE.relative_to(PUZZLES_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
