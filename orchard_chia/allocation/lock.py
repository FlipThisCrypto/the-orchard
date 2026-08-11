# SPDX-License-Identifier: Apache-2.0
"""A single-holder lock, so two payout runs cannot overlap.

There is no mutual exclusion anywhere else in this codebase — no flock, no
pidfile, no filelock — and for read-mostly jobs that is fine. It is not fine
here. The scheduler fires on a timer and the operator runs the command by hand,
and those two things WILL coincide eventually. When they do, both processes
read the audit store, both find no instruction for a wallet, and both send. The
idempotency key does not save you: it is checked before the insert, and two
processes can both pass a check that neither has yet invalidated.

The lock is a file created with O_EXCL, which is atomic on both NTFS and POSIX
and needs no dependency. It holds the pid and a start time so a human looking
at a stuck lock can tell whether the holder is alive.

STALE LOCKS ARE NOT BROKEN AUTOMATICALLY BY DEFAULT
===================================================

A lock left by a killed process looks exactly like a lock held by a process
that is mid-spend. Breaking it automatically is the same mistake as retrying an
unknown outcome — it optimises for the run continuing rather than for money not
moving twice. ``break_after_seconds`` exists for the scheduler, where an
unbounded stale lock would wedge the service forever, but it defaults to off
and the caller has to choose it deliberately.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class LockBusy(RuntimeError):
    """Someone else holds the lock. Never subclass this into a retry."""


@dataclass
class RunLock:
    path: Path
    break_after_seconds: int | None = None

    _fd: int | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return "(unreadable)"

    def _holder_pid(self) -> int | None:
        """The pid recorded in the lock file, if parseable."""
        for token in self._holder().split():
            if token.startswith("pid="):
                try:
                    return int(token[4:])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Best-effort liveness. POSIX: signal 0. Windows: never called with a
        signal here — os.kill(pid, 0) on Windows can TERMINATE the process, so
        the open-handle unlink refusal below is the liveness check there."""
        import sys
        if sys.platform == "win32":
            return False        # defer to the unlink refusal
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True         # exists, owned by someone else
        return True

    def _age_seconds(self) -> float:
        try:
            return max(0.0, (datetime.now(timezone.utc).timestamp()
                             - self.path.stat().st_mtime))
        except OSError:
            return 0.0

    def acquire(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = self._age_seconds()
            if self.break_after_seconds is not None and age > self.break_after_seconds:
                # Deliberate, bounded, and loud: the caller asked for this.
                #
                # But never break a LIVE holder, however old the lock. On
                # Windows the open handle makes unlink fail below, which was
                # the only protection — and on POSIX unlink succeeds on an
                # open file, so a long-running holder's lock would have been
                # broken and a second writer admitted. CI's Linux runners
                # caught exactly that. The pid check makes the protection
                # explicit on POSIX; Windows keeps the handle semantics.
                pid = self._holder_pid()
                if pid is not None and self._pid_alive(pid):
                    raise LockBusy(
                        f"{self.path} is {age / 60:.1f} min old and past the "
                        f"break threshold, but its holder (pid {pid}) is still "
                        f"alive — long-running is not stale. Refusing to break "
                        f"a live lock.")
                try:
                    self.path.unlink()
                except OSError as e:
                    # Windows refuses to delete a file that is still open, which
                    # makes the failure informative rather than annoying: it means
                    # the holder is ALIVE, and a live holder is exactly when
                    # breaking the lock would be the wrong thing to do. Say so
                    # instead of letting the retried open() raise FileExistsError
                    # from a line that reads like a bug.
                    raise LockBusy(
                        f"{self.path} is {age / 60:.1f} min old and past the "
                        f"break threshold, but could not be removed ({e}). The "
                        f"holder ({self._holder()}) still has it open, so it is "
                        f"running — not stale. Refusing to break a live lock."
                    ) from None
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise LockBusy(
                    f"another allocation run holds {self.path} "
                    f"(held by {self._holder()}, {age / 60:.1f} min old). "
                    f"Refusing to run concurrently — two runs can both pass the "
                    f"duplicate check and both send. If that process is dead, "
                    f"delete the file by hand after confirming no spend is in "
                    f"flight."
                ) from None
        os.write(self._fd, f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n"
                 .encode("utf-8"))
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *a) -> None:
        self.release()
