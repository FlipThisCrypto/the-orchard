<!-- SPDX-License-Identifier: Apache-2.0 -->
# Oracle schema migrations (Alembic)

Versioned, Postgres-ready migrations for the oracle DB (T4 / ADR-0004 D4).
Run everything from the **repo root** — `alembic.ini` lives there and the DB
URL comes from the oracle's own settings (`ORCHARD_ORACLE_DB_URL` /
`oracle/.env`), so migrations always target the same DB the service uses.

## Two ways the schema gets created

- **`db.create_all()`** runs on app startup and is the zero-config path for
  development and tests — it creates any missing tables plus a couple of
  idempotent additive `ALTER`s. Great for "just run the oracle locally."
- **Alembic** (this directory) is the versioned path for anything that holds
  real data — production, and the SQLite→Postgres move. The baseline migration
  builds the *same* schema `create_all()` does (asserted in
  `oracle/tests/test_migrations.py`), so the two never diverge.

## Bringing a DB under Alembic control

**Fresh DB (nothing created yet):**
```bash
alembic upgrade head        # creates the full schema + stamps it at head
```

**Existing DB (already built by create_all — e.g. the current dev/prod DB):**
```bash
alembic stamp head          # record "already at the baseline", create nothing
```
The schemas are identical, so stamping is safe — it just tells Alembic where
the DB is so future `upgrade`s apply cleanly.

## Making a schema change from here on

No more hand-rolled `ALTER`s in `db.py` — change the models, then:
```bash
# 1. edit oracle/app/models.py
alembic revision --autogenerate -m "what changed"   # 2. generate
#    review the generated file in versions/ (autogenerate is a draft)
alembic upgrade head                                 # 3. apply
```
CI guards this: `test_migrations.py` runs `alembic check`, which fails if the
models and migrations drift apart (a model column with no matching migration).

## Postgres

Set a Postgres URL and install a driver — no code change:
```bash
pip install "psycopg[binary]>=3.1"
export ORCHARD_ORACLE_DB_URL=postgresql+psycopg://user:pass@host/orchard
alembic upgrade head
```
Migrations render in SQLite-batch mode but run unchanged on Postgres. The
`last_seq` replay check uses a guarded `UPDATE ... WHERE last_seq < :seq`, so
it stays correct with multiple Postgres workers.
