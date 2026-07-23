# Orchard verification API

> The Orchard's promise is that **anyone can verify the oracle without our
> services**. This is the public Python surface for doing so — used by
> `orchard-verify`, and available to Keepers, the Atlas, and researchers.
> Everything here is pure (no network) except `fetch_bundle` and the live
> inclusion path, which speak Chia's DataLayer/full-node RPC (shapes in
> [`CHIA_DATALAYER_RPC.md`](CHIA_DATALAYER_RPC.md)).

All records follow [`SPEC.md`](../SPEC.md); signatures are secp256r1 over
canonical JSON (SPEC §0/§4).

## Whole-season bundle — `orchard_chia.datalayer.verify`

```python
verify_bundle(*, meta, node, attest, readings_records,
              expect_full_season=True) -> Report
```
Runs every SPEC §7 check over a bundle and returns a `Report` (`.valid`,
`.checks[]` of `{name, ok, detail}`, `.as_dict()`). **Never raises** on bad
data — corruption/tampering surfaces as a failed check, not an exception. The
checks: record consistency (same node·season), schema/signer-scheme support,
device signatures (every reading), anti-backdate anchor presence, full Merkle
proofs (every reading), hour roots, and — for a full season — season root,
`verified_hours`, `season_score`, and the oracle season signature.
`expect_full_season=False` marks a known partial slice (skips the three
season-level checks).

```python
verify_reading_in_hour(reading, node_pubkey, hour_record) -> ReadingCheck
```
Verify **one** reading (SPEC §8): device signature, Merkle membership in its
hour tree, and hour-root recompute. `ReadingCheck.ok` / `.as_dict()`.

```python
verification_badge(report, *, sealed=True, stale=False, unverifiable=False) -> str
```
The SPEC §8 public badge: `Verified` | `Live` | `Partial` | `Stale` |
`Unverified` (precedence: unverifiable → stale → validity).

## On-chain inclusion — `orchard_chia.datalayer.inclusion`

```python
check_inclusion(rpc, store_id, key_hex_list, *, expected_values=None) -> InclusionReport
```
SPEC §7 check 1 against a DataLayer node: `get_root` must be **confirmed**,
`get_proof` must cover every key, `verify_proof` must report `current_root`, and
(with `expected_values`, a `key_hex → value_hex` map) each key's on-chain
`value_clvm_hash` must equal `clvm_hash(value)`. `InclusionReport.cannot_verify`
distinguishes transient/unprovable (retry) from a value mismatch (tampering).
`key_clvm_hash(key_hex)` = `sha256(0x01‖bytes)` maps a proof entry back to a key.

## Anti-backdate — `orchard_chia.datalayer.block_anchor`

```python
anchor_matches(block_record, anchor, ts_ms) -> bool
find_anchor_block(block_records, anchor, ts_ms) -> dict | None
```
SPEC §4.2 kernel: a block whose `header_hash` starts with the 16-hex anchor
prefix and whose `timestamp ≤ ts_ms` bounds the reading's creation time from
below. (The offline verifier checks anchor presence/format; wiring this kernel
to a live full-node window is a deferred step — SPEC §7.)

## Bundle assembly — `orchard_chia.datalayer.fetch`

```python
fetch_bundle(rpc, store_id, *, node_id, season, hours=None) -> dict
```
Assemble a `verify_bundle`-shaped dict from a live store (discovers all present
hours when `hours=None`). Raises `FetchError` on a missing required key.

## Primitives — `orchard_chia.datalayer.schema`

`verify_reading(reading, pubkey)` · `verify_attest(attest, pubkey)` ·
`hour_root(readings)` · `season_root(hour_roots_by_hour)` ·
`verified_hours(by_hour, pubkey)` · `season_score(verified_hrs)`. All
recomputable by anyone from public data.

## CLI — `python -m orchard_chia.cli.orchard_verify`

- `vectors <path>` — verify the golden bundle offline.
- `live --node-id ID --season N [--hour H] [--json]` — fetch + verify a season
  (or a partial `--hour` slice) with on-chain inclusion.
- `reading --node-id ID --season N --hour H --ts-ms T [--json]` — verify one
  reading.

Exit codes: **0** VALID · **1** INVALID (a definitive contradiction) · **2**
CANNOT-VERIFY (transient/unprovable — retry, not fraud).
