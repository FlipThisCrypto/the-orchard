# Flashing one Tree for verifiable readings (firmware 0.6.0)

The one physical step between the merged code and real, provable sensor data on
chain. Until a Tree signs its readings there is nothing publishable: the
publisher **discards unsigned readings and never re-signs them**, which is
exactly the property that makes the dataset verifiable rather than merely
published — the oracle must not be able to manufacture data it then vouches for.

## The one thing that must not go wrong

**Do not erase flash.** The Tree's identity lives in NVS:

| NVS key | What it is | If lost |
|---|---|---|
| `node_id` | the Tree's 32-hex identity | it becomes a different Tree; re-registration needed |
| P-256 private key | its device provenance key | its published pubkey no longer matches; past readings unverifiable |
| HMAC secret | transport auth to the oracle | `/readings` returns 401 |
| claim nonce | the Orchard Pass binding | Pass link broken |

A normal PlatformIO upload preserves all of it. `esptool erase_flash`, a
"Erase Flash: Always" setting, or a full-chip erase **destroys it**.

Good news, verified: **firmware 0.5.1 already generates and stores the P-256
key** — the merged `identity.cpp` is identical to production's on that point. So
every live Tree already carries its provenance key and simply doesn't sign with
it yet. Flashing 0.6.0 changes behaviour, not identity, and needs no
re-registration.

## Before you flash

Record the pubkey so you can prove it survived. Over the serial console:

```text
PUBKEY
```

Copy that value. It should be 66 lowercase hex characters starting `02` or `03`
(a compressed secp256r1 point).

## Build and flash

The build is already verified on this machine:
`freenove_esp32_wroom`, 1,072,288 bytes, flash 81.3% used.

```powershell
cd firmware
python -m platformio run                          # build (env freenove_esp32_wroom)
python -m platformio run --target upload          # flash over USB — NOT erase_flash
```

For an S3 board use `-e freenove_esp32s3` (or `-e freenove_esp32s3_uart`).

## Immediately after flashing

1. **Identity survived** — over serial, `PUBKEY` must return the *same* value
   you recorded. If it changed, NVS was wiped; stop and say so before the Tree
   re-registers under a new id.
2. **Version took** — the boot banner / `HW_INFO` should report `0.6.0`.
3. **It is still reporting.** Within a few minutes:

   ```bash
   curl -s https://oracle.theorchard.network/nodes/<NODE_ID> | python -m json.tool
   ```

   `last_reading_at` should be recent, and `fw_version` `0.6.0`.
4. **The oracle learned the key** — same response:

   ```text
   "device_pubkey": "02…"      # 66 hex chars, matching PUBKEY above
   ```

   The oracle lifts this out of the first signed reading. Until it appears, the
   publisher will refuse that node's hours as unverifiable — which is correct
   behaviour, not a fault.

## Then publish

Wait for at least one **fully closed** UTC hour (only closed hours are
published), then on the operator machine:

```powershell
python -m orchard_chia.datalayer preflight
python -m orchard_chia.datalayer publish --dry-run
python -m orchard_chia.datalayer publish
```

See [`DATALAYER_GO_LIVE.md`](DATALAYER_GO_LIVE.md) for what to expect, the fee,
and how to verify the result.

## If the Tree stops reporting

The most likely cause is the oracle URL in NVS. The merged firmware keeps
production's behaviour — it appends `/readings` to the configured **base** URL,
and defensively strips a trailing `/readings` from units that were provisioned
with the full path. Both forms work. Check the configured URL over serial before
suspecting the firmware.
