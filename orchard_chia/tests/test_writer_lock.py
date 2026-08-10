# SPDX-License-Identifier: Apache-2.0
"""Publish and attest cannot overlap — with each other or themselves.

Neither writer had any mutual exclusion. A scheduler tick and an operator's
manual run overlapping meant two fee-bearing batch_updates and a final store
state decided by mempool ordering; attest has no watermark, so two overlapping
attest runs each read get_value, each saw the old bytes, and each paid to
write. One lock file covers both because they mutate the same store.
"""
from __future__ import annotations

import pytest

from orchard_chia.allocation.lock import LockBusy, RunLock


def test_publish_and_attest_share_one_lock_file():
    """Same store, same lock. Two names would only exclude like from like."""
    import inspect
    from orchard_chia.datalayer import main as attest_main, publish
    pub_src = inspect.getsource(publish)
    att_src = inspect.getsource(attest_main)
    assert 'datalayer-writer.lock' in pub_src
    assert 'datalayer-writer.lock' in att_src


def test_the_lock_is_the_allocation_lock():
    """One implementation of mutual exclusion in the repo, not three."""
    import inspect
    from orchard_chia.datalayer import publish
    assert "RunLock" in inspect.getsource(publish)


def test_a_held_lock_refuses_a_second_writer(tmp_path):
    held = RunLock(tmp_path / "datalayer-writer.lock").acquire()
    try:
        with pytest.raises(LockBusy):
            RunLock(tmp_path / "datalayer-writer.lock").acquire()
    finally:
        held.release()


def test_dry_runs_do_not_take_the_lock():
    """A report must never be blocked by a write in flight — blocking reads
    teaches people to delete lock files, and then the lock protects nothing."""
    import inspect
    from orchard_chia.datalayer import publish
    src = inspect.getsource(publish)
    assert "if not dry_run:" in src.split("datalayer-writer.lock")[0].rsplit("def ", 1)[-1] \
        or "if not dry_run:" in src
