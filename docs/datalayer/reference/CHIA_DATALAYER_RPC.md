# Chia DataLayer — official RPC & CLI reference (vendored)

> **What this is.** An authoritative, in-repo copy of the Chia DataLayer RPC and
> CLI surface that The Orchard integrates against, transcribed from the official
> sources below. It exists so our integration is written against *documented*
> shapes rather than guessed ones, and so a reviewer can check our code without
> leaving the repo.
>
> **This is reference, not aspiration.** Where our code disagrees with what Chia
> actually returns, that is a bug in our code — see [§7 Orchard integration
> mapping](#7-orchard-integration-mapping), which names each gap and the file to
> fix.

**Sources (pulled 2026-07-23):**

- DataLayer RPC — <https://docs.chia.net/reference-client/rpc-reference/datalayer-rpc/>
- DataLayer CLI — <https://docs.chia.net/reference-client/cli-reference/datalayer-cli/>
- Proof-of-inclusion design — chia-blockchain PR #16845,
  <https://github.com/Chia-Network/chia-blockchain/pull/16845>
- RPC overview / conventions — <https://docs.chia.net/rpc/>

**Applies to:** Chia reference client (mainnet) DataLayer service. Re-verify these
shapes against the pinned `chia-blockchain` version before relying on any field;
Chia's RPC response shapes have drifted across releases (a fact our own
`inclusion.py` comments already lament).

---

## 1. Transport & conventions

- **Default DataLayer RPC port:** `8562`, configured under `data_layer.rpc_port`
  in `~/.chia/mainnet/config/config.yaml`. (Full-node RPC is `8555`; wallet
  `9256`.)
- **Transport:** HTTPS `POST` with a JSON body, secured by **mutual TLS**. The
  caller presents the service's client cert + key (paths under `~/.chia/mainnet/
  config/ssl/data_layer/`). Local connections are loopback-only, so the reference
  client's self-signed CA is why callers use `verify=False` on `127.0.0.1` — and
  *only* there (our `rpc.py` refuses `verify=False` off-loopback by design).
- **Every response is a JSON object with a `success` boolean.** `success: false`
  carries an `error` string; treat it as a failed call.
- **Keys and values are hex-encoded byte blobs.** DataLayer neither interprets
  nor validates the bytes — structure (our `readings:`/`node:`/`attest:` ASCII
  namespace) is entirely an application convention.

---

## 2. Store read endpoints

### `get_root`
Current on-chain root of a store the node **owns**. (For a *subscribed* store use
`get_local_root`; `get_root` can return an invalid hash for subscriptions.)

- **Request:** `id` (TEXT, required) — hex store ID.
- **Response:**
  | field | type | meaning |
  |---|---|---|
  | `success` | bool | |
  | `confirmed` | bool | root is confirmed on-chain (vs. pending tx) |
  | `hash` | string | root hash, hex |
  | `timestamp` | integer | Unix seconds of the confirming block |

### `get_value`
Value for one key.

- **Request:** `id` (TEXT, req), `key` (TEXT, req, hex), `root_hash` (TEXT, opt —
  pin the read to a specific root for reproducibility).
- **Response:** `success` (bool), `value` (string, hex; absent/`null` if the key
  is not present).

### `get_keys`
All keys in a store.

- **Request:** `id` (req), `root_hash` (opt), `page` (NUMBER, opt — enables
  pagination), `max_page_size` (NUMBER, opt — bytes/page, default **40 MB**).
- **Response:** `success`, `keys` (array of hex strings), `total_pages`,
  `total_bytes` (last two only when paginating).

### `get_keys_values`
Every key/value pair (store must be owned or subscribed).

- **Request:** `id` (req), `root_hash` (opt), `page`/`max_page_size` (opt).
- **Response:** `success`, `keys_values` (array of objects with `key`, `value`
  (both hex), `hash`, `atom` (null)), plus `total_pages`/`total_bytes` when
  paginating.

### `get_kv_diff`
Changed keys between two roots.

- **Request:** `id` (req), `hash_1` (req), `hash_2` (req), `page`/`max_page_size`
  (opt).
- **Response:** `success`, `diff` (array of `{key, value, type}` where `type` is
  `"INSERT"` or `"DELETE"`).

### `get_sync_status`
Local vs. on-chain progress for a store.

- **Request:** `id` (req).
- **Response:** `success`, `sync_status` object with `generation`, `root_hash`,
  `target_generation`, `target_root_hash`. Store is caught up when
  `generation == target_generation`.

---

## 3. Store write endpoint

### `batch_update`
Apply a changelist to a store; by default submits an on-chain transaction.

- **Request:**
  - `id` (TEXT, req) — hex store ID.
  - `changelist` (req) — JSON array of operations:
    - `{"action": "insert", "key": "<hex>", "value": "<hex>"}`
    - `{"action": "delete", "key": "<hex>"}`
    - A key change is **delete-then-insert** of the same key (idempotent replace).
  - `submit_on_chain` (BOOLEAN, opt, default **`True`**) — when `False`, stages the
    change locally without a transaction (batch several, submit once).
  - `fee` (TEXT, opt) — transaction fee in **mojos**.
- **Response:** `success`, `tx_id` (string, present when submitted on-chain).

---

## 4. Proof of inclusion

The primitive behind SPEC §7 check 1 ("on chain & unchanged"). Design per
PR #16845.

**CLVM-hash rule.** To keep proofs small, a proof carries only the *CLVM hashes*
of key and value, not the bytes:

```
clvm_hash(atom_bytes) = sha256( 0x01 || atom_bytes )
```

So the hash of one of our keys is `sha256(b"\x01" + bytes.fromhex(key_hex))`.
That is how a proof entry (which lists no key/value) is matched back to a known
key — recompute the key's CLVM hash and compare to `key_clvm_hash`.

### `get_proof`
Generate a Merkle inclusion proof for one or more keys.

- **Request:**
  - **`store_id`** (TEXT, req) — hex store ID. **Note the name: `store_id`, not
    `id`** (every other DataLayer endpoint uses `id`; this one does not).
  - `keys` (STRING LIST, req) — hex keys to prove.
- **Response:**
  ```json
  {
    "success": true,
    "proof": {
      "coin_id": "<hex>",
      "inner_puzzle_hash": "<hex>",
      "store_proofs": {
        "store_id": "<hex>",
        "proofs": [
          {
            "key_clvm_hash": "<hex>",
            "value_clvm_hash": "<hex>",
            "node_hash": "<hex>",
            "layers": [ /* sibling hashes: the Merkle path to the root */ ]
          }
        ]
      }
    }
  }
  ```
  One entry in `store_proofs.proofs` per requested key. `layers` is the path of
  sibling hashes from the leaf `node_hash` up to the store root.

### `verify_proof`
Validate a proof against the **current on-chain root**. Requires only a single
root lookup — **no store sync or subscription needed**, which is exactly why a
stranger with any full node can run it.

- **Request:**
  - `coin_id` (STRING, req) — from the proof's `proof.coin_id`.
  - `inner_puzzle_hash` (STRING, req) — from `proof.inner_puzzle_hash`.
  - `store_proofs` (req) — the `proof.store_proofs` object (`store_id` +
    `proofs[]`).
- **Response:**
  | field | type | meaning |
  |---|---|---|
  | `success` | bool | |
  | `current_root` | bool | **the meaningful bit.** `true` ⇒ the proof chains to the *currently published* root, so the data is present now. `false` ⇒ the root moved since the proof was generated; inclusion at the current root is **not** asserted. |
  | `verified_clvm_hashes` | object | `store_id` + `inclusions[]` of `{key_clvm_hash, value_clvm_hash}` actually verified |

**Interpretation for a verifier.** A green inclusion is `success == true` **and**
`current_root == true` **and** the requested key's CLVM hash appears in
`verified_clvm_hashes.inclusions`. Treat `current_root == false` as
"cannot-verify-now" (SPEC's *Unverified* badge), not as "invalid".

---

## 5. Subscriptions & mirrors (Keeper path — ADR-0003 §8)

### `subscribe` / `unsubscribe`
- `subscribe`: `id` (req), `urls` (req; may be empty to rely on mirrors) → `success`.
- `unsubscribe`: `id` (req), `retain` (opt — keep local files) → `success`.

### `add_mirror` / `get_mirrors`
- `add_mirror`: `id` (req), `urls` (req), `amount` (INTEGER, req — mojos committed;
  higher = higher discovery priority), `fee` (opt). On-chain tx. → `success`.
- `get_mirrors`: `id` (req) → `success` + mirror list.

### `get_local_root`
Local root of a subscribed store (counterpart to `get_root` for owned stores).
- `id` (req) → `success`, `hash`.

---

## 6. CLI equivalents (`chia data …`)

For operators and manual verification. Flag names differ from the RPC param
names — do not assume they match.

| Subcommand | Purpose | Key flags |
|---|---|---|
| `create_data_store` | Create a new store (on-chain) | `-m/--fee`, `-f/--fingerprint` |
| `update_data_store` | Apply a changelist (= `batch_update`) | `--id`, `-d/--changelist`, `--submit` |
| `get_value` | One value by key | `--id`, `--key`, `-r/--root_hash` |
| `get_keys` | List keys | `--id`, `-p/--page`, `--max-page-size` |
| `get_keys_values` | List key/value pairs | `--id`, `-r/--root_hash`, `-p/--page` |
| `get_root` | Current root + timestamp | `--id` |
| `get_proof` | Generate inclusion proof | `--id`, `-k/--key` |
| `verify_proof` | Verify a proof, no sync | `-p/--proof` (JSON) |
| `subscribe` | Track a remote store | `--id`, `-u/--url` (repeatable) |
| `unsubscribe` | Stop tracking | `--id`, `--retain` |
| `get_sync_status` | Local vs on-chain root | `--id` |
| `add_mirror` | Advertise a mirror | `-i/--id`, `-a/--amount`, `-u/--url` |
| `get_mirrors` | List mirrors | `-i/--id` |

> CLI `get_proof` takes `--id` even though the **RPC** endpoint takes `store_id`.
> The mismatch is real; it is not a transcription error.

---

## 7. Orchard integration mapping

Where our code touches each endpoint, and the gaps this reference exposes.

| Endpoint | Our caller | Status |
|---|---|---|
| `batch_update` | `datalayer/rpc.py::DataLayerRpc.batch_update` | OK. Does not yet pass `submit_on_chain`/`fee` — acceptable (defaults submit on-chain). |
| `get_value` | `rpc.py::get_value` / `get_value_strict` | OK. |
| `get_keys` | `rpc.py::get_keys` | OK. Does not paginate — fine below the 40 MB page cap; revisit at scale. |
| `get_root` | `rpc.py::get_root`, `datalayer/inclusion.py` | OK. `hash` is the correct field; the `root_hash`/`root` fallbacks in `inclusion.py` are dead branches (Chia returns `hash`). |
| `get_proof` | `rpc.py::get_proof`, `inclusion.py::check_inclusion` | OK (param + parsing fixed — see below). |
| `verify_proof` | `rpc.py::verify_proof`, `inclusion.py::check_inclusion` | OK — inclusion now requires `current_root` (SPEC §7 check 1). |

**Defects this reference exposed — all now closed:**

1. ~~**`get_proof` sends the wrong param name.**~~ Was posting `{"id": ...}`; the
   endpoint requires **`store_id`**. Fixed 2026-07-23 (commit `13622aa`), pinned
   by `test_rpc_body.py`.
2. ~~**`inclusion.py` parses a response shape Chia never returns.**~~ The old
   `_count_proven_keys` guessed at a top-level `proofs` dict / `proof` list and,
   failing that, counted every key as proven. Rewritten against
   `proof.store_proofs.proofs[]`, matching keys by CLVM hash `sha256(0x01 ||
   key_bytes)`; no blanket fallback. Fixed 2026-07-23 (commit `296afa0`).
3. ~~**No `verify_proof` call.**~~ `check_inclusion` now calls `verify_proof` and
   requires `current_root == true`; a moved root or unreachable endpoint reports
   an honest cannot-verify rather than a false *Verified*. Fixed 2026-07-23.

Together these turned `orchard-verify live`'s inclusion step from an RPC-envelope
gesture into a real on-chain "unchanged under the current root" check.

---

## 8. Change log

| Date | Change |
|---|---|
| 2026-07-23 | Initial vendoring from docs.chia.net + PR #16845 (RPC, CLI, proof-of-inclusion, integration mapping). |
