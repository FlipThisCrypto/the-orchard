# ADR-0006: Sensor data-quality classification + payload schema versioning

- **Status:** Accepted (2026-06-15) — codifies already-settled policy (the
  deferred list: "sensor-data plausibility gating of rewards → v2") and adds a
  cheap forward-compat field now (HANDOVER T14).
- **Date:** 2026-06-15
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Related:** ADR-0003 (verifiable dataset / integer fixed-point payloads),
  ADR-0008 (heartbeat memos commit a sensor-batch hash), HANDOVER deferred list.

## Context

Trees carry sensors of very different trustworthiness, and v1 rewards are
**uptime-based** — not data-quality-based. Two things need writing down before
the fleet and the published dataset grow:

1. **Not all sensor values are equal.** A BME280 reports calibrated digital
   temperature/humidity/pressure; an MQ-135 reports an uncalibrated analog gas
   voltage that drifts, needs burn-in, and is at best *indicative*. Presenting
   both as equally authoritative would misrepresent the dataset.
2. **The payload format will change.** A mixed-firmware fleet (we already run
   0.4.0 → 0.4.8 in the field) needs a way to evolve reading formats without
   the oracle/verifier mis-parsing older or newer bodies.

## Decision

### Sensor grade
Classify each sensor as one of:

- **trusted-grade** — digital, factory-calibrated, deterministic. Example:
  BME280 (temperature, humidity, pressure). Suitable as authoritative
  environmental data.
- **indicative** — uncalibrated / analog / drift-prone. Example: MQ-135 gas
  (raw ADC / mV). Stored and displayed, but **labeled indicative** and never
  presented as calibrated absolute values.

The grade is metadata about the sensor *type*, recorded in the dataset's
sensor descriptors (the `node:` record's sensor list, ADR-0003), so any
consumer can tell which readings are authoritative.

### Burn-in
Gas sensors (MQ-135 class) require a documented **burn-in** (≈24–48 h powered)
before their output is meaningful. This goes in the operator docs (quickstart /
wiring), not in firmware.

### Rewards (restated, settled)
**v1 rewards = verified uptime only.** Data-quality / plausibility gating of
rewards is **v2** (Keeper layer territory, ADR-0008). This ADR does not change
the reward model; it records why uptime-only is the correct v1 scope given the
sensor-grade reality above.

### Payload schema versioning
Add an integer **`schema`** field to every reading payload, starting at
**`schema: 1`** for the current format. The oracle stores it per reading. This
is cheap now and lets a mixed-firmware fleet evolve payload formats safely:
consumers branch on `schema`, and a missing `schema` is treated as the
pre-versioning format (`0`/legacy).

## Consequences

- The dataset is honest about which values are authoritative vs indicative,
  without blocking collection of either.
- `schema` is a one-line firmware addition (alongside `seq`/`ts`) and a stored
  column/field on the oracle — implemented as the T14 follow-up, kept out of
  this decision doc so the policy and the code land separately.
- No reward-model change; this ADR is the reference for *why* v1 stays
  uptime-only and the anchor for the future v2 data-quality work.
