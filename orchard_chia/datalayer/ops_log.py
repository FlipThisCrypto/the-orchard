# SPDX-License-Identifier: Apache-2.0
"""Structured operator journal for DataLayer jobs (publish / attest).

Writes one JSON object per line to ``orchard_chia/data/ops/<job>.jsonl``
so cron failures, dry-runs, and seal sources are reconstructable without
scraping Task Scheduler output. Never logs secrets or signing keys.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_OPS_DIR = Path(__file__).resolve().parents[1] / "data" / "ops"


def _ops_dir() -> Path:
    raw = os.environ.get("ORCHARD_OPS_LOG_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_OPS_DIR


@dataclass
class OpsRun:
    """One job invocation — start + end (or fail) are always paired."""
    job: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    t0: float = field(default_factory=time.monotonic)
    fields: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _path: Path | None = field(default=None, repr=False)
    _finished: bool = field(default=False, repr=False)

    def note(self, event: str, **kwargs: Any) -> None:
        """Append a mid-run event (also written immediately for crash safety)."""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job": self.job,
            "run_id": self.run_id,
            "event": event,
            **{k: v for k, v in kwargs.items() if _safe(v)},
        }
        self.events.append(row)
        _append(self._path, row)

    def finish(self, status: str, **kwargs: Any) -> None:
        """Terminal event: status is ok | error | dry_run | noop."""
        self._finished = True
        duration_ms = int((time.monotonic() - self.t0) * 1000)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job": self.job,
            "run_id": self.run_id,
            "event": "finish",
            "status": status,
            "duration_ms": duration_ms,
            "started_at": self.started_at,
            **self.fields,
            **{k: v for k, v in kwargs.items() if _safe(v)},
        }
        _append(self._path, row)


def _safe(v: Any) -> bool:
    """Drop values that look like secrets or are non-JSON-serializable noise."""
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        # Never log long hex blobs (keys/sigs).
        if len(v) >= 64 and all(c in "0123456789abcdefABCDEF" for c in v):
            return False
        if "key" in v.lower() and len(v) > 40:
            return False
        return True
    if isinstance(v, (list, dict)):
        return True
    return False


# Rotate when a journal exceeds this many bytes (default 5 MiB).
_MAX_BYTES = int(os.environ.get("ORCHARD_OPS_LOG_MAX_BYTES", str(5 * 1024 * 1024)))


def _rotate_if_needed(path: Path) -> None:
    """Rename oversized journal to ``.1`` (single generation) then start fresh."""
    try:
        if not path.is_file() or path.stat().st_size < _MAX_BYTES:
            return
        rotated = path.with_suffix(path.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError:
        pass


def _append(path: Path | None, row: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"[orchard.ops] WARN: could not write {path}: {e}", file=sys.stderr)


@contextmanager
def ops_run(job: str, **fields: Any) -> Iterator[OpsRun]:
    """Context manager: start event on enter; finish on clean exit / error on raise.

    Usage::

        with ops_run("publish", dry_run=True, trees=3) as run:
            run.note("harvest", hours=2)
            ...
    """
    path = _ops_dir() / f"{job}.jsonl"
    run = OpsRun(job=job, fields={k: v for k, v in fields.items() if _safe(v)})
    run._path = path
    start = {
        "ts": run.started_at,
        "job": job,
        "run_id": run.run_id,
        "event": "start",
        **run.fields,
    }
    _append(path, start)
    try:
        yield run
    except Exception as e:
        run.finish("error", error=type(e).__name__, error_msg=str(e)[:200])
        raise
    else:
        # Clean exit but the caller never finalized — write a terminal event so
        # the journal never has an orphan 'start' that looks like a crash.
        if not run._finished:
            run.finish("incomplete")
