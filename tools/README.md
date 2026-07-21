# tools/ — Operator and CI utilities

| Tool | Purpose |
|------|---------|
| `python -m tools.oracle_backup` | SQLite online backup, integrity_check, restore-drill |
| `python -m tools.tree_sim.sim` | Virtual Trees: functional / load / negative modes |
| `python tools/sign_release.py` | Sign release `.bin` with P-256 (CI secret or local key file) |
| `python tools/verify_flasher_manifest.py` | Validate `flasher/manifest.json` (+ optional GitHub asset HEAD) |
| `powershell -File tools/preflight_orchard.ps1` | Live surface preflight (home, flash, claim, health, view) |

Run from the **repo root**. Tests for these tools live next to them as `test_*.py`
and are included via `pyproject.toml` `testpaths`.

See also:

- [`docs/ops/ORACLE_BACKUP_RESTORE.md`](../docs/ops/ORACLE_BACKUP_RESTORE.md)
- [`docs/ops/REQUIRE_SEQ_FLIP.md`](../docs/ops/REQUIRE_SEQ_FLIP.md)
- [`docs/security/SIGNED_OTA.md`](../docs/security/SIGNED_OTA.md)
- [`tools/tree_sim/README.md`](tree_sim/README.md)
