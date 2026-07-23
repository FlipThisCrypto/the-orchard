# SPDX-License-Identifier: Apache-2.0
"""Structured DataLayer ops journal tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchard_chia.datalayer import ops_log


def test_ops_run_start_and_finish(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    with ops_log.ops_run("publish", dry_run=True, trees=2) as run:
        run.note("harvest", batches=1)
        run.finish("ok", plan_hours=1)

    path = tmp_path / "publish.jsonl"
    assert path.exists()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["event"] == "start"
    assert lines[0]["dry_run"] is True
    assert lines[1]["event"] == "harvest"
    assert lines[1]["batches"] == 1
    assert lines[2]["event"] == "finish"
    assert lines[2]["status"] == "ok"
    assert lines[2]["duration_ms"] >= 0
    assert "run_id" in lines[0]


def test_ops_run_records_error_on_exception(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        with ops_log.ops_run("attest") as run:
            run.note("mid")
            raise RuntimeError("boom")

    lines = [
        json.loads(x)
        for x in (tmp_path / "attest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lines[-1]["event"] == "finish"
    assert lines[-1]["status"] == "error"
    assert lines[-1]["error"] == "RuntimeError"


def test_ops_run_auto_finishes_when_caller_forgets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    with ops_log.ops_run("publish", trees=1) as run:
        run.note("mid")
        # caller never calls run.finish()

    lines = [
        json.loads(x)
        for x in (tmp_path / "publish.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lines[-1]["event"] == "finish"
    assert lines[-1]["status"] == "incomplete"


def test_ops_run_does_not_double_finish(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    with ops_log.ops_run("publish") as run:
        run.finish("ok")

    lines = [
        json.loads(x)
        for x in (tmp_path / "publish.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    finishes = [ln for ln in lines if ln["event"] == "finish"]
    assert len(finishes) == 1
    assert finishes[0]["status"] == "ok"


def test_ops_log_strips_long_hex_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    secret = "ab" * 32
    with ops_log.ops_run("publish", signing_key=secret, trees=1) as run:
        run.finish("ok", oracle_sig=secret, plain="fine")
    text = (tmp_path / "publish.jsonl").read_text(encoding="utf-8")
    assert secret not in text
    assert "fine" in text

def test_ops_log_rotates_when_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_OPS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(ops_log, "_MAX_BYTES", 50)
    path = tmp_path / "publish.jsonl"
    path.write_text("x" * 80, encoding="utf-8")
    with ops_log.ops_run("publish") as run:
        run.finish("ok")
    assert (tmp_path / "publish.jsonl.1").exists()
    assert path.exists()
    assert "finish" in path.read_text(encoding="utf-8")
