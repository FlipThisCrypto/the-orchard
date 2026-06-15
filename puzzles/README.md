<!-- SPDX-License-Identifier: Apache-2.0 -->
# `puzzles/` — ChiaLisp toolchain (HANDOVER T19)

The on-chain track (ADR-0008): ChiaLisp puzzles that let Trees prove uptime as
on-chain singleton heartbeats and let operators claim from epoch vaults — no
oracle in the reward path. This directory is the toolchain and test harness
those puzzles are built and verified with.

- **`hashlock`** — toolchain starter (T19), not production.
- **`tree_heartbeat`** — the Tree-singleton heartbeat inner puzzle (T20):
  secp256r1-authorized, timelocked self-recreation that advances a monotonic
  counter and announces each heartbeat. Behavioral tests (signed by the live
  `schema` signer, run under the consensus VM) live in
  `orchard_chia/tests/test_heartbeat_puzzle.py`; security model in
  [SECURITY-NOTES.md](SECURITY-NOTES.md).
- **epoch vault** — T22, not yet implemented (see SECURITY-NOTES.md).

```
puzzles/
├── src/*.clsp         # puzzle sources (hashlock, tree_heartbeat)
├── build.py           # compile -> pin hex + treehash into hashes.json
├── hashes.json        # committed bytecode + sha256 + on-chain treehash (pinned)
├── tests/             # run compiled puzzles under the consensus VM (chia_rs)
├── SECURITY-NOTES.md  # per-puzzle threat model + ported claim-race analysis
└── requirements.txt   # clvm_tools + chia_rs (+ pytest)
```

## Workflow

```bash
pip install -r puzzles/requirements.txt
python puzzles/build.py            # (re)compile src/*.clsp -> hashes.json
python puzzles/build.py --check    # CI mode: fail if hashes.json drifted
python -m pytest puzzles/tests     # run the puzzles under the VM
```

Edit a `.clsp`, re-run `build.py`, commit the updated `hashes.json`. CI (the
`puzzles` job) runs `--check` + the VM tests on every push, so bytecode can't
change unnoticed.

## Conventions

- **Compile, then pin.** Every puzzle's serialized CLVM, its sha256, and its
  **treehash** (sha256tree — the actual on-chain puzzle hash) are committed in
  `hashes.json`. Reviewers diff bytecode, not just source; CI fails on drift.
- **Curry the parameters that define identity.** Values that make a coin what
  it is (a Tree's pubkey, an epoch id, a vault's treasury hash) are *curried
  in* — they become part of the puzzle and therefore the puzzle hash. The
  solution carries only per-spend data (signatures, chosen conditions).
- **Announcements: name them, don't collide them.** When the heartbeat and
  vault puzzles coordinate via `CREATE`/`ASSERT_*_ANNOUNCEMENT`, prefix the
  announced message with a domain tag (e.g. `'claim'`, `'epoch'`) so puzzles
  can't be tricked into consuming each other's announcements.
- **secp256r1 is the device-auth primitive.** On-chain verification of a
  Tree's reading/heartbeat signature uses CLVM `secp256r1_verify` (ADR-0007);
  it's already exercised end-to-end in
  `orchard_chia/tests/test_clvm_secp.py`, and the heartbeat puzzle (T20) will
  build on it.
- **Value-holding puzzles get review + testnet soak** before mainnet — porting
  the Merkle-vault snapshot/claim-race analysis (ADR-0008 §2). `hashlock` here
  is only a toolchain starter, not a production puzzle.
