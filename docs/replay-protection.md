# Replay protection for `/readings`

**The problem.** The HMAC proves a reading was produced by the Tree's secret,
but nothing makes it single-use. `ts_ms` is `millis()` (monotonic per boot,
never validated), and uptime credit only requires one valid POST per hour. So
anyone who captures one signed body off the wire (plain HTTP) can replay it
every hour, forever, and farm $JUICE from a Tree that's been in a drawer for
months.

**The fix.** A monotonically increasing sequence number, persisted on the Tree
in NVS, included in the signed body, and enforced as strictly-increasing by the
oracle. An old captured body carries an old `seq`, so replays are rejected. No
clock sync needed — which matters, since the Tree only has reliable UTC when
GPS has a fix.

NVS wear note: writing flash on every reading (e.g. every 60 s) is ~0.5M
writes/year. The implementation below uses a **reservation block**: it persists
`seq + 256` whenever the live counter crosses the stored watermark, so NVS is
written once per 256 readings while still guaranteeing the counter never
repeats across reboots or crashes (a crash just skips up to 255 numbers, which
is fine — the oracle only requires "greater than last seen").

---

## 1. `firmware/src/identity.h` — add the declaration

After the `hmac_sha256` declaration:

```cpp
// Monotonically increasing sequence number, persisted to NVS.
// Survives reboots and crashes (reservation-block scheme: NVS is
// written once per kSeqReserve calls, and on boot the counter resumes
// from the persisted watermark — possibly skipping numbers, never
// repeating one). Include in every signed payload; the oracle rejects
// any submission whose seq is not strictly greater than the last
// accepted one, which kills replay attacks.
uint32_t next_seq();
```

## 2. `firmware/src/identity.cpp` — implement it

Add to the anonymous namespace (next to the other `kNvsKey*` constants):

```cpp
constexpr const char* kNvsKeySeq = "seq_wm";   // persisted watermark
constexpr uint32_t kSeqReserve   = 256;         // NVS write amortization

uint32_t seq_counter_   = 0;   // live counter (RAM)
uint32_t seq_watermark_ = 0;   // highest value guaranteed unused after a crash
```

Add this near the bottom of the file (outside the anonymous namespace, inside
`orchard::identity`):

```cpp
uint32_t next_seq() {
  // Lazy init on first call: resume from the persisted watermark.
  // Everything below the watermark may already have been used before
  // the last reboot/crash, so we start AT the watermark.
  if (seq_watermark_ == 0) {
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
    seq_watermark_ = prefs.getUInt(kNvsKeySeq, 0);
    if (seq_watermark_ == 0) {
      // First boot ever: claim the first block.
      seq_watermark_ = kSeqReserve;
      prefs.putUInt(kNvsKeySeq, seq_watermark_);
    }
    prefs.end();
    seq_counter_ = seq_watermark_ - kSeqReserve;
  }

  ++seq_counter_;

  // Crossing the reservation boundary: persist the next block before
  // handing out a number from it.
  if (seq_counter_ >= seq_watermark_) {
    seq_watermark_ = seq_counter_ + kSeqReserve;
    Preferences prefs;
    prefs.begin(ORCHARD_NVS_NAMESPACE, /*readOnly=*/false);
    prefs.putUInt(kNvsKeySeq, seq_watermark_);
    prefs.end();
  }

  return seq_counter_;
}
```

## 3. `firmware/src/net/oracle.cpp` — include seq in the signed body

In `oracle_post_reading`, where the identity fields are added:

```cpp
  payload["node_id"] = identity::node_id_hex();
  payload["fw"]      = orchard::kFirmwareVersion;
  payload["ts_ms"]   = (uint32_t)millis();
  payload["seq"]     = identity::next_seq();   // <-- NEW: replay protection
```

It's inside the body, so it's covered by the existing HMAC — an attacker can't
bump the seq on a captured packet without invalidating the signature.

---

## 4. `oracle/app/models.py` — track the high-water mark per Node

Add to the `Node` class (next to `fw_version`):

```python
    # Replay protection: highest `seq` value accepted from this Tree.
    # The firmware persists a monotonic counter in NVS and includes it
    # in every signed body; /readings rejects anything <= this value.
    # Reset to 0 on re-registration (the NVS-wipe recovery path).
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

**Migration for existing databases** — `create_all()` won't add columns to an
existing table. Until Alembic lands, a one-liner does it:

```bash
sqlite3 path/to/oracle.db "ALTER TABLE nodes ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0;"
```

## 5. `oracle/app/routes/readings.py` — enforce it

In `post_reading`, after the JSON parse succeeds and before building the
`Reading` row:

```python
    # ---- Replay protection -------------------------------------------
    # `seq` is inside the HMAC-covered body, so it can't be forged.
    # Require it to be a strictly increasing integer per Tree. Old
    # firmware that doesn't send seq is rejected once the oracle is
    # upgraded — flash Trees first, or stage with a settings flag.
    seq = payload.get("seq") if isinstance(payload, dict) else None
    if not isinstance(seq, int) or seq <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing or invalid 'seq' (firmware too old? reflash)",
        )
    if seq <= node.last_seq:
        # Same signed body seen before (or out-of-order duplicate).
        # 409 rather than 401: the signature was VALID, the content is
        # just stale. Don't credit uptime, don't store the row.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"replayed or out-of-order seq {seq} (last accepted {node.last_seq})",
        )
    node.last_seq = seq
    # ------------------------------------------------------------------
```

Note on concurrency: SQLite serializes writers, so two simultaneous replays
can't both pass the check within one oracle process. If you ever move to
Postgres + multiple workers, change this to a guarded
`UPDATE nodes SET last_seq = :seq WHERE node_id = :id AND last_seq < :seq`
and reject when zero rows are affected.

## 6. `oracle/app/routes/register.py` — recovery path

A Tree whose NVS is wiped restarts its counter near zero, and the oracle would
then reject everything it sends. Re-registration is already the recovery ritual
for a wiped Tree, so reset the watermark there. In the handler where an
**existing** node is updated (the `new: false` path), add:

```python
        node.last_seq = 0   # NVS wipe recovery: Tree counter restarted
```

This is safe because /register is itself protected (wallet session + Pass
verification), so an attacker can't reset the counter just by replaying
readings.

## 7. Test to add to `oracle/tests/test_oracle.py`

```python
def test_reading_replay_rejected(client, registered_node):
    node_id, secret = registered_node
    body = json.dumps({"seq": 1, "sensors": {}}).encode()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    headers = {"X-Orchard-Node": node_id, "X-Orchard-Sig": sig}

    first = client.post("/readings", content=body, headers=headers)
    assert first.status_code == 202

    # Exact same signed body again -> replay -> 409, no uptime credit.
    replay = client.post("/readings", content=body, headers=headers)
    assert replay.status_code == 409

    # seq lower than last accepted -> also rejected.
    body2 = json.dumps({"seq": 0, "sensors": {}}).encode()
    sig2 = hmac.new(secret, body2, hashlib.sha256).hexdigest()
    stale = client.post("/readings", content=body2,
                        headers={"X-Orchard-Node": node_id, "X-Orchard-Sig": sig2})
    assert stale.status_code in (400, 409)
```

(Adapt the fixture names to however `test_oracle.py` registers a node — I
didn't read the whole suite.)

---

## Rollout order

1. Ship oracle changes with the seq check **behind a settings flag**
   (`require_seq: false` initially) or accept missing seq with a deprecation
   log line, mirroring how you staged `require_wallet_session`.
2. Flash all Trees with the new firmware.
3. Flip enforcement on. From that point, captured packets are worthless.

One thing this deliberately does *not* solve: a malicious operator's own Tree
lying about its sensors. That's the Keeper/validator layer (v2) and
sensor-plausibility checks — different threat, different fix.
