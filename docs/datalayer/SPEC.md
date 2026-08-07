# The Orchard — DataLayer publish schema (v1.0)

> **Design tenet (the one that governs every choice below):**
> **The Orchard should not ask people to trust our oracle. It should let them
> _verify_ the oracle.** Anything the oracle claims — a reading, an uptime, a
> reward score — must be independently recomputable from public, signed,
> on-chain-anchored data. If a value can't be verified by a stranger with a
> Chia node, it doesn't belong in the public surface.

Concrete, buildable companion to
[ADR-0003](../decisions/0003-datalayer-verifiable-dataset.md). Defines the key
namespace, every record format, how it's signed, how the Merkle commitments are
built, how often it's written, how the public dashboard reads it, and how anyone
verifies it. Reuses the canonicalization rule already in
`orchard_chia/datalayer/attest.py` so existing code carries forward.

> **Wire-level RPC/CLI shapes** (what `get_proof`/`verify_proof`/`batch_update`
> actually accept and return) are transcribed from the official Chia docs in
> [`reference/CHIA_DATALAYER_RPC.md`](reference/CHIA_DATALAYER_RPC.md). The live
> verifier (§7 check 1) is written against that reference. The **public
> verification API** anyone can build on is documented in
> [`reference/VERIFY_API.md`](reference/VERIFY_API.md).

**Schema version:** `1.1.0` (1.1.0 added the attest verification-basis fields —
§2.4; same major, so 1.x verifiers and pre-1.1 records keep working).
**Status:** namespace frozen; two signing details still open (Season-signature
scheme, block-anchor source — see ADR-0003 open questions). Not yet built.

---

## 0. Conventions (shared rules)

- **Canonical JSON** (one rule, reused everywhere): `json.dumps(obj,
  sort_keys=True, separators=(",", ":"))` then UTF-8 encode. No whitespace,
  keys sorted. Identical to `attest._canonical_bytes`.
- **Hex:** keys and values handed to DataLayer are hex-encoded UTF-8. Hashes /
  sigs are lowercase hex in JSON unless noted.
- **`<NODE_ID>`:** 32 uppercase hex chars (16 bytes), as today.
- **`<SEASON>`:** zero-padded 8-digit decimal (`00000005`), as today.
- **`<HOUR>`:** zero-padded 2-digit UTC hour bucket within the Season,
  `00`..`23` (maps 1:1 onto the oracle's existing per-hour uptime buckets).
- **Time:** ISO-8601 UTC with `Z`. Readings also carry `ts_ms` (device epoch
  millis) for ordering.
- **Numbers — integer fixed-point ONLY (the rule that makes signing portable):**
  signatures cover canonical *bytes*, so every signer must render a number
  identically. Python emits `1.0` for `1.00`; C++/JS may differ — so **floats
  are forbidden anywhere under a signature.** Every signed measurement is an
  **integer** in a fixed-point unit (see the metric table in §2.3). The
  dashboard multiplies by the per-key scale in `meta:schema.units` to show human
  values. **Enforced in code:** `schema.reject_floats()` runs inside every
  `_sign()`, so a float in a signed payload raises rather than producing a
  signature no one else can reproduce. `bool` is allowed (`true`/`false` is
  byte-stable). Unsigned config (e.g. the `units` scales in `meta:`) may use
  any type.

---

## 1. Store layout (key namespace)

One store per operator. Keys are colon-delimited ASCII (readable on
inspection), hex-encoded for DataLayer. Additive over today's `attest:`.

| Key | Cardinality | Mutability | Purpose |
|---|---|---|---|
| `meta:schema` | 1 | rare | Self-description: version, units, geohash precision, signer scheme, store role. |
| `node:<NODE_ID>` | 1 per Tree | on change | The Tree "card": pubkey, board, sensors, coarse location. |
| `readings:<NODE_ID>:<SEASON>:<HOUR>` | 1 per node·season·hour | **append-only** | That hour's device-signed readings + the hour's Merkle root. **The permanent data.** |
| `attest:<NODE_ID>:<SEASON>` | 1 per node·season | sealed once | Summary: uptime, Season Merkle root, Season score, oracle signature. **Reward anchor.** |
| `latest:<NODE_ID>` | 1 per Tree | every cycle | Denormalized "now" pointer: most recent signed reading + counters. **Live view cache.** |

**Invariant: the namespace _is_ the public surface.** Anything not under
`meta:` / `node:` / `readings:` / `attest:` / `latest:` is not published (see
§9). `readings:` rows are never rewritten — that's what makes the historical
record permanent; `latest:` is the only routinely-overwritten key.

---

## 2. Record formats

### 2.1 `meta:schema`

```json
{
  "orchard_schema": "1.1.0",
  "store_role": "orchard-operator",
  "operator_pass_nft": "<launcher-id or null>",
  "units": {
    "temperature_mc":     {"display": "°C",    "pow10": -3},
    "humidity_milli_pct": {"display": "%RH",   "pow10": -3},
    "pressure_pa":        {"display": "hPa",   "pow10": -2},
    "gas_adc_raw":        {"display": "adc",   "pow10":  0},
    "gas_mv":             {"display": "mV",    "pow10":  0},
    "pm25_ugm3_x100":     {"display": "µg/m³", "pow10": -2},
    "pm10_ugm3_x100":     {"display": "µg/m³", "pow10": -2},
    "gps_sats":           {"display": "sats",  "pow10":  0}
  },
  "geohash_precision": 5,
  "signer": { "device_sig": "secp256r1", "season_sig": "secp256r1", "season_pubkey": "<oracle compressed SEC1 pubkey, 66 hex>" },
  "writer_version": "<semver>",
  "created_at": "2026-06-09T00:00:00Z"
}
```

### 2.2 `node:<NODE_ID>`

```json
{
  "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
  "pubkey": "<secp256r1 compressed SEC1 public key, 66 hex>",
  "board": "freenove-esp32s3-uart",
  "fw": "0.4.7",
  "sensors": [
    {"name": "mq135",  "unit": "adc", "active": true},
    {"name": "bme280", "bus": "i2c",  "active": true},
    {"name": "gps",    "bus": "uart", "active": true}
  ],
  "geohash": "dr5ru",
  "first_seen_utc": "2026-05-28T20:43:27Z",
  "label": null
}
```

`pubkey` is the provenance anchor — every reading from this node must verify
against it. `geohash` precision matches `meta:schema.geohash_precision`.

### 2.3 `readings:<NODE_ID>:<SEASON>:<HOUR>` (the data)

```json
{
  "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
  "season": 5,
  "hour": 13,
  "count": 60,
  "readings": [
    {
      "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
      "ts_ms": 1749480000123,
      "block_anchor": "a1b2c3d4e5f60718",
      "metrics": {
        "temperature_mc": 21400,
        "humidity_milli_pct": 48200,
        "pressure_pa": 101260,
        "gas_adc_raw": 1234,
        "gas_mv": 994,
        "gps_fix": true,
        "gps_sats": 7
      },
      "sig": "<secp256r1 r||s over sha256(canonical(reading-without-sig)), 128 hex>"
    }
  ],
  "hour_root": "<sha256 hex — Merkle root over this hour's leaves>"
}
```

**Metric keys (integer fixed-point only — §0).** A Tree emits the subset its
sensors support; the dashboard renders `raw * 10**pow10` from `meta:schema.units`.

| Metric key | Meaning | Encoding | Example | Human |
|---|---|---|---|---|
| `temperature_mc` | air temperature | milli-°C | `21400` | 21.4 °C |
| `humidity_milli_pct` | relative humidity | milli-% | `48200` | 48.2 %RH |
| `pressure_pa` | barometric pressure | pascals | `101260` | 1012.60 hPa |
| `gas_adc_raw` | MQ gas, raw ADC | counts | `1234` | — |
| `gas_mv` | MQ gas, voltage | millivolts | `994` | 0.994 V |
| `pm25_ugm3_x100` | PM2.5 | ×100 µg/m³ | `1234` | 12.34 µg/m³ |
| `pm10_ugm3_x100` | PM10 | ×100 µg/m³ | `1850` | 18.50 µg/m³ |
| `gps_fix` | GPS fix | bool | `true` | yes |
| `gps_sats` | satellites in view | count | `7` | 7 |

- The **device signs the per-reading object with `sig` removed** (§4). The
  writer never alters a reading — it only packages what the device signed.
- `gas_mv = round(gas_adc_raw * 3300 / 4095)` (integer); `voltage_v` is never
  signed — the dashboard derives volts from `gas_mv` for display.
- `block_anchor` = first 8 bytes (16 hex) of a recent Chia header hash (§4.2).
- Precise GPS lat/lon are **omitted by default** (`gps_fix`/`gps_sats` only)
  unless the operator opts in (§9).

### 2.4 `attest:<NODE_ID>:<SEASON>` (sealed summary)

Backward-compatible superset of today's record. New fields **bold**:

```json
{
  "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
  "season": 5,
  "season_start_utc": "2026-05-31T00:00:00Z",
  "season_end_utc": "2026-06-01T00:00:00Z",
  "hours_online": 24,
  "verified_hours": 24,
  "seal_source": "readings",
  "sigs_verified": true,
  "root_mismatches": 0,
  "season_score": 100,
  "reading_count": 1440,
  "block_height_at_write": 8794728,
  "data_hash": "<= season_root (kept so reader.py keeps working)>",
  "season_root": "<sha256 hex — Merkle root over present hour_roots>",
  "signed_at": "2026-06-01T00:05:00Z",
  "oracle_sig": "<signature over canonical(payload-without-oracle_sig)>"
}
```

`data_hash` is retained (don't break the payout `reader.py`) but now equals
`season_root`. `oracle_sig` migrates HMAC → secp256r1 (pubkey in
`meta:schema.signer`). See §3 for `verified_hours` / `season_score`.

**Verification basis (1.1.0) — the record must not overstate itself.** These
three fields are *inside* the signature, so an intermediary cannot strip the
caveat and leave the number looking proven:

| Field | Meaning |
|---|---|
| `seal_source` | `"readings"` — `season_root` is a real Merkle root over published device-signed readings, so `verified_hours` is recomputable by anyone. `"placeholder"` — **nothing was published**; the root is only `sha256(node:season:hours)` and **`verified_hours` is 0 because nothing was verified**. |
| `sigs_verified` | `false` when hours were counted by reading *presence* (no device pubkey was available to check signatures against). |
| `root_mismatches` | Hours whose stored `hour_root` disagreed with a recompute. `>0` is a tampering/corruption red flag. |

> **Why a placeholder writes `verified_hours: 0`.** Writing the oracle's
> `hours_online` there would sign a self-report into a field named *verified*,
> in the one case with no evidence at all — the exact thing §3 exists to
> prevent. The claim is still recorded in `hours_online`; the payout falls back
> to it for a **declared** placeholder (so reward amounts are unchanged) and
> labels the row `unverified`. A pre-1.1.0 record declares no basis and keeps
> its previous treatment.

`orchard-verify` reports a placeholder record as **cannot-verify (exit 2)** —
it is honestly labelled, not fraudulent — while a declared `root_mismatches > 0`
is a definitive **INVALID (exit 1)**.

### 2.5 `latest:<NODE_ID>` (live pointer)

```json
{
  "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
  "season": 5,
  "hour": 13,
  "last_sealed_season": 4,
  "running_hours_online": 13,
  "last_reading": { "...": "a full signed reading object (as in §2.3)" },
  "updated_at": "2026-05-31T13:00:30Z"
}
```

`last_reading` is itself a signed reading, so it's verifiable exactly like a
`readings:` entry. But `latest:` is **overwritten every cycle**, so its
on-chain inclusion proof is transient — the permanent copy lives in
`readings:`. The dashboard uses `latest:` for the "now" view; verification of
history uses `readings:`/`attest:`.

---

## 3. Uptime vs. Season score (the tenet, made concrete)

Two distinct numbers, deliberately separated so a stranger can catch a lying
oracle:

- **`hours_online`** — the oracle's *claimed* count of hours the node was up.
- **`verified_hours`** — hours that contain **at least one device-signature-valid
  reading**, recomputed from the public `readings:` rows. *Anyone* can derive
  this; it requires no trust.
- **`season_score`** — the public, recomputable reward metric.
  **v1 (integer, round-half-up so every language agrees):**
  `season_score = (100 * verified_hours + 12) // 24`.

> **Why two numbers:** the dashboard shows both. If `season_score` (verified)
> equals the oracle's `hours_online`-derived figure, the oracle is honest for
> that node·season. If `verified_hours < hours_online`, the oracle over-counted
> — and the discrepancy is provable, not alleged. That single comparison *is*
> "verify the oracle."

**Constraint for all future score multipliers** (sensor diversity, geographic
scarcity, Pass tier, reputation — the hooks already noted in the payout
calculator): any factor folded into `season_score` **must be deterministically
recomputable from public data**, or it breaks the tenet and may not enter the
score. Tier/identity multipliers that depend on private state belong in a
*separate, clearly-labeled* payout adjustment, not in the verifiable score.

> **Known limitation — reading↔hour-bucket binding (open).** `verified_hours`
> counts an hour bucket as verified if it holds **any** signature-valid reading,
> but does **not** currently check that the reading's `ts_ms` actually falls in
> that UTC hour. A device signs `{node_id, ts_ms, block_anchor, metrics}`; the
> signature is valid regardless of which `readings:<…>:<HOUR>` bucket the writer
> files it under. So an oracle could copy one genuine reading into all 24 hour
> buckets and forge 100 % uptime — the recomputed score would still "verify."
> (The golden vectors reflect this: their `ts_ms` maps to UTC hour 14 while the
> record sits in bucket 13, so the invariant is demonstrably not enforced today.)
>
> **Mitigations (need a design decision, hence not silently applied):**
> (a) verifier rejects any reading whose `hour_of_ts_ms(ts_ms)` ≠ bucket hour
>     (and `season_number_for(ts_ms)` ≠ bucket season); (b) the per-hour Merkle
>     leaf domain-separates on `(season, hour)` so a leaf can't move buckets.
> Either changes the frozen cross-language vectors (byte-pinned for firmware),
> so it must be coordinated with a schema/vectors bump — tracked here rather
> than patched under an autonomous change.

---

## 4. Signing

### 4.1 Device signature (secp256r1 — ADR-0007)

1. Build the reading object **without** `sig`.
2. `msg = canonical(reading_without_sig)` (§0); `digest = sha256(msg)`.
3. `sig = ecdsa_p256_sign(device_sk, digest)` — **RFC 6979 deterministic**
   nonce, encoded as fixed **64-byte `r||s`** (two 32-byte big-endian
   integers), **low-S normalized** (`s ≤ n/2`); store lowercase hex in `sig`.
4. Verify: standard P-256 ECDSA of `(node.pubkey, digest, sig)` — including
   on-chain: CLVM's `secp256r1_verify` consumes exactly
   `(pubkey33, digest32, sig64)`.

Encodings: `pubkey` is the **33-byte compressed SEC1** point, lowercase hex
(66 chars); `sig` is 128 hex chars. Determinism note: RFC 6979 means the same
key + same bytes ⇒ the same signature, in firmware (mbedTLS) and Python
(`ecdsa`) alike — the golden vectors pin this byte-for-byte.

Private key generated on first boot, stored in NVS, never transmitted. Public
key exported via `HW_INFO` and sent to the oracle at registration → written to
`node:<NODE_ID>.pubkey`.

### 4.2 Block anchor (anti-backdate)

- The Tree fetches a recent Chia header hash from the oracle's `/beacon`
  endpoint (v1) — oracle proxies its full node's `get_blockchain_state`.
- It includes the first 16 hex chars in `block_anchor` **before signing**.
- Verifier confirms `block_anchor` prefixes a real header whose `timestamp ≤`
  the reading's `ts_ms` → the reading was created no earlier than that block.
- *Trust nuance:* the oracle picks *which* recent block but cannot forge one,
  so the lower bound holds. v1.x lets the device hit a public RPC directly.

### 4.3 Oracle / Season signature

`oracle_sig` covers `canonical(attest_payload_without_oracle_sig)` (today's
rule). Scheme moves HMAC → secp256r1 (same §4.1 encodings — one curve
everywhere), publicly verifiable against `meta:schema.signer.season_sig` + a
published oracle pubkey. (ADR-0003's open question — ed25519 vs. BLS vs.
drop — was settled by ADR-0007: secp256r1, for CLVM verifiability.)

---

## 5. Merkle construction (pin this exactly)

Domain-separated, SHA-256, deterministic.

```
leaf_hash(reading)  = sha256( 0x00 || canonical(reading_with_sig) )
node_hash(l, r)     = sha256( 0x01 || l || r )
```

- **Ordering:** readings within an hour sorted by `(ts_ms, sig)` ascending
  before hashing.
- **Odd level:** the last node is **promoted** (carried up unchanged), not
  duplicated.
- **`hour_root`** = Merkle root over that hour's `leaf_hash`es.
- **`season_root`** = Merkle root over the **present** `hour_root`s in ascending
  `hour` order (empty hours skipped, not zero-filled). Hour roots are the season
  tree's leaves **directly** (not re-hashed), so a one-hour Season has
  `season_root == hour_root` and the reading→Season proof concatenates cleanly.
- Full reading→Season proof = (reading's path to `hour_root`) ++ (`hour_root`'s
  path to `season_root`).
- Empty hour / store: root = `sha256(0x00)` sentinel constant, never empty.

---

## 6. Publish frequency & writer behavior

| Job | Trigger | Writes |
|---|---|---|
| Hot path | hourly (configurable 15m / 1h / season) | `readings:<N>:<S>:<H>` for the just-closed hour, per active node; `latest:<N>`; `node:<N>` if changed |
| Sealed | Season close (~4608 blocks / ~24h) | `attest:<N>:<S>` (root + uptime + score + sig) |
| Schema | writer version / precision change | `meta:schema` |

- **One `batch_update` per cycle** carrying all changed keys (insert, or
  delete-then-insert on change — same idempotency as today).
- **Watermark (new):** the writer tracks the last published
  `(node, season, hour)` so it doesn't re-scan from genesis each hour. SQLite
  alongside the payout watermark (`orchard_chia/data/`, gitignored).
- **Cost envelope (measure before scaling):** hourly batching → ~24 tx/day/
  operator regardless of node count (all nodes share the cycle's batch). Raw
  ≈ 100–150 KB/node/day.

---

## 7. Verification algorithm (`orchard-verify` CLI + Atlas "Verify")

Runnable by anyone with a Chia node or public RPC — never trusting our
services:

1. **Inclusion / permanence** — `get_proof` for the `readings:` key; verify
   against the store's on-chain root. Show root + `block_height` + Spacescan
   link to the store singleton's coin.
2. **Device provenance** — fetch `node:<NODE_ID>.pubkey`; P-256-ECDSA-verify
   the reading's `sig` over `sha256(canonical(reading_without_sig))`.
3. **Membership** — recompute `leaf_hash`, walk the Merkle path to `hour_root`,
   then to `attest:<NODE_ID>:<SEASON>.season_root`.
4. **Anti-backdate** — confirm `block_anchor` matches a real header with
   `timestamp ≤ ts_ms`.
5. **Uptime / score recompute** — fetch all `readings:<NODE_ID>:<SEASON>:*`,
   verify each device sig, count `verified_hours`, check `season_score` and that
   the recomputed `season_root` matches `attest`.

Checks 1–4 validate a single datum; check 5 validates the reward. All green =
"on Chia, unchanged since block N, signed by the device, not back-dated, and
the score is honest."

**Implemented (Phase 1, offline):**
`python -m orchard_chia.cli.orchard_verify vectors <vectors.json>` runs the
device-signature, Merkle (proof + hour root + season root), verified-hours,
season-score, and oracle-season-signature checks against a published bundle —
i.e. checks **2, 3, 5** plus signature verification, and the **offline half of
check 4** (block-anchor *presence & format*: every reading must carry a
well-formed, non-placeholder 16-hex anchor). The oracle pubkey comes from
`meta:schema.signer.season_pubkey`; the device pubkey from `node:.pubkey`.
Exit 0 = VALID, 1 = INVALID, 2 = cannot-verify.

**Implemented (live):** `orchard-verify live` runs check **1** on-chain —
`get_root` must report a **confirmed** root, `get_proof` must cover every key
the verdict trusts (`meta`/`node`/`attest`/`readings`), `verify_proof` must
report `current_root == true`, and each key is value-bound to the record via
`value_clvm_hash` (see `datalayer/inclusion.py`,
[`reference/CHIA_DATALAYER_RPC.md`](reference/CHIA_DATALAYER_RPC.md) §4).

**Still Phase 2 (live, deferred):** the *chain-lookup* half of check **4** —
resolving a reading's `block_anchor` prefix to a real block with
`timestamp ≤ ts_ms` — needs full-node access (and `/beacon` + firmware anchors)
and is not yet wired.

---

## 8. Public dashboard surface — "Orchard Atlas" (field → source)

Every public field maps to a published key, so the dashboard is a *renderer of
verifiable data*, not a source of truth.

| Public field | Source |
|---|---|
| **Node ID** | `node:<NODE_ID>.node_id` (= the key) |
| **Coarse location** | `node:<NODE_ID>.geohash` (precision from `meta:schema`) |
| **Sensors installed** | `node:<NODE_ID>.sensors[]` |
| **Last reading** | `latest:<NODE_ID>.last_reading` (ts + values) |
| **Last verified publication** | timestamp of the newest `readings:`/`attest:` key whose inclusion proof verified (derived in §7.1) |
| **Uptime** | `attest:<…>.hours_online` (sealed) + `latest:<…>.running_hours_online` (current season) |
| **Season score** | recomputed `season_score` (§3), shown beside the oracle's claim |
| **Verification status** | computed badge (below) from the §7 checks |
| **DataLayer store ID** | the store being read (operator/site-level; chain-resolvable) |
| **Join instructions** | static site element from docs + `meta:schema.store_role` |

**Verification status badges:**
- **Verified** — latest sealed Season: inclusion OK, sampled device sigs OK,
  `season_root` matches, `season_score == ` oracle claim.
- **Live** — current Season in progress; readings present + signed, not yet
  sealed.
- **Partial** — some readings failed a check, or `verified_hours < hours_online`
  (oracle over-count flagged).
- **Stale** — no reading within the staleness window (node offline).
- **Unverified** — store/proof unreachable; nothing asserted.

---

## 9. Privacy — private by default

Published only if the operator explicitly opts in; otherwise **never written to
any key**:

| Private field | Where it actually lives | In DataLayer? |
|---|---|---|
| **Precise GPS** (lat/lon) | oracle SQLite (raw reading); opt-in publish only | No (coarse geohash only) |
| **Wallet address** | oracle DB; store *ownership* is chain-resolvable but the address is never in JSON | No |
| **IP / network info** | oracle DB / device | No |
| **WiFi / device secrets** (device `sk`, oracle key, SSID/pass) | device NVS / oracle host; never transmitted | No |
| **Raw private admin logs** | oracle host / dashboard logs | No |

Reinforced by the §1 invariant: the five public namespaces are the *entire*
public surface. There is no "accidental" leakage path — if it's not a defined
key, it isn't on chain.

---

## 10. Join flows (summary; full UX TBD)

- **Tree operator** — hold an Orchard Pass → run the oracle → wizard register →
  oracle auto-creates / reuses the store and binds `dl_store_id` to the Node row
  (ADR-0002 path a) → writer publishes `node:`/`readings:`/`attest:`/`latest:`.
- **Keeper / Mirror** — `chia data subscribe <store_id>` + `chia data
  add_mirror` → serve others' content for durability; v2 earns $JUICE. Discover
  stores via the Orchard Pass NFT collection + a future `/network/stores`.
- **Consumer / researcher** — Atlas, `orchard-verify`, or a read/export API. No
  permission, no account.

---

## 11. Component contract (why this schema is the foundation)

The schema is the interface every other component reads or writes through:

| Key | Written by | Read by |
|---|---|---|
| `meta:schema` | writer (oracle host) | Atlas, verify CLI |
| `node:<NODE_ID>` | writer (from oracle registration + device `HW_INFO`) | Atlas, verify CLI |
| `readings:<…>` | writer (hourly, from device-signed readings) | Atlas, verify CLI, **payout recompute** |
| `attest:<…>` | writer (Season close) | **payout**, Atlas, verify CLI |
| `latest:<NODE_ID>` | writer (each cycle) | Atlas (live) |

Upstream, the **device** produces the secp256r1 keypair, the signed readings,
and the pubkey that seed `node:`/`readings:`. Downstream, **rewards** are computed
by recomputing `season_score` from `readings:` — so even payouts obey the tenet.

---

## 12. Build checklist

**Reference implementation (done — this is the contract everything else conforms to):**
- [x] Merkle module — `orchard_chia/datalayer/merkle.py`.
- [x] Schema: keys, record builders (`meta`/`node`/`readings`/`attest`/`latest`),
      secp256r1 device + Season signing (RFC 6979), roots,
      `verified_hours`/`season_score` — `orchard_chia/datalayer/schema.py`.
- [x] Golden cross-language vectors — `orchard_chia/datalayer/testdata/vectors.json`
      (regenerate via `python -m orchard_chia.datalayer._gen_vectors`).
- [x] Tests: Merkle KATs + proofs, sign/verify round-trips, score recompute,
      value round-trips, **no-signed-floats guard** — `test_merkle.py`,
      `test_schema.py` (101 pass).
- [x] **Signed reading core is integer fixed-point** (§0 / §2.3 table); floats
      rejected in code by `schema.reject_floats()`; vectors regenerated.

**Frozen decisions (2026-06-09, curve amended 2026-06-12 by ADR-0007):**
reading format = integer fixed-point only · hourly `readings:` batches ·
overwritable `latest:` · append-only history · recomputable `season_score` ·
**device + Season signature = secp256r1, RFC 6979, r||s low-S** (BLS deferred,
not dropped) · **block anchor = oracle `/beacon`** for v1, direct RPC later.

**Remaining (build order):**
- [x] `orchard-verify` CLI — Phase 1 offline verifier
      (`orchard_chia/datalayer/verify.py` + `orchard_chia/cli/orchard_verify.py`,
      `test_verify_cli.py`). `vectors` runs all 7 checks on the golden bundle →
      VALID; tampering → INVALID; `live` is an interface-frozen stub.
- [x] `orchard-verify live` — inclusion wired: `get_root` must be **confirmed**,
      `get_proof` covers every verdict-bearing key (`meta`/`node`/`attest`/
      `readings`), `verify_proof` requires `current_root`, and each key is
      value-bound via `value_clvm_hash` (`datalayer/inclusion.py`). Exit codes
      distinguish INVALID (1) from cannot-verify (2). Partial `--hour` slices
      skip season-level checks. Per-reading `orchard-verify reading` (SPEC §8).
- [x] Anti-backdate check — offline **presence/format** of `block_anchor`
      (`verify.py`); chain-lookup **kernel** built (`datalayer/block_anchor.py` +
      full-node block RPCs). *Deferred:* the live anchor→block orchestration
      (needs a synced node + `/beacon` + firmware anchors).
- [ ] Firmware: secp256r1 keygen in NVS, sign each reading, `HW_INFO` exports
      pubkey, fetch `/beacon` for `block_anchor`.
- [ ] Oracle: store device pubkey at registration; `/beacon` endpoint; persist
      per-reading `sig` + `block_anchor`.
- [x] Writer: hot path + watermark wired into `orchard_chia/datalayer/publish.py`
      (closed-hour harvest, idempotent `batch_update` with configurable `fee`,
      post-write confirm, `publish_watermark.db`). Sealed path in `main.py`.
- [x] `orchard-verify` offline checks — device sig (all readings), full Merkle
      proofs, hour/season roots, `verified_hours`/`season_score`, oracle sig,
      record consistency, schema/scheme compatibility.
- [ ] Orchard Atlas: read-only DataLayer reader + map + per-field §8 mapping +
      Verify. (Building blocks ready: `verify.verification_badge` for §8 badges,
      `verify.verify_reading_in_hour` for per-reading Verify.)
- [x] `reader.py` back-compat — reads `attest:` rows; payout pays on the
      verifiable `verified_hours` when present, else `hours_online`.

**Known limitation:** reading↔hour-bucket binding — see §3.
