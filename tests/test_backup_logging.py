"""Logging tests for the backup-restore endpoint (api/backup.py).

Issue #205 Unit E: `restore_backup` used to have zero server-side logging —
a failed disaster-recovery restore left no trace beyond the client's own
error. This is a rare, high-stakes operation, so it must always leave a
trail: the attempt itself (INFO, before restoring) plus the outcome
(INFO on success, WARNING for a missing backup, .exception() for anything
else).
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.backup as backup_mod
from api.backup import router as backup_router
from api.deps import get_current_user, require_admin


def _make_app(user_payload: dict) -> FastAPI:
    # These tests cover logging only; the admin gate itself is tested against
    # a real DB in test_backup_api.py.
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user_payload
    app.dependency_overrides[require_admin] = lambda: user_payload
    app.include_router(backup_router)
    return app


@pytest.fixture
def client():
    return TestClient(_make_app({"sub": "42", "email": "admin@example.com"}))


def test_successful_restore_logs_attempt_and_success(client, monkeypatch, caplog):
    monkeypatch.setattr(backup_mod, "restore_db", lambda date: None)

    with caplog.at_level(logging.INFO, logger="api.backup"):
        resp = client.post("/api/backup/2026-01-01/restore")

    assert resp.status_code == 200

    messages = [r.getMessage() for r in caplog.records]
    assert any("requested" in m and "2026-01-01" in m and "42" in m for m in messages)
    assert any("succeeded" in m and "2026-01-01" in m for m in messages)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_missing_backup_logs_attempt_and_warning(client, monkeypatch, caplog):
    def _raise(date):
        raise FileNotFoundError(date)

    monkeypatch.setattr(backup_mod, "restore_db", _raise)

    with caplog.at_level(logging.INFO, logger="api.backup"):
        resp = client.post("/api/backup/2000-01-01/restore")

    assert resp.status_code == 404

    messages = [r.getMessage() for r in caplog.records]
    assert any("requested" in m and "2000-01-01" in m for m in messages)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2000-01-01" in warnings[0].getMessage()


def test_unexpected_failure_logs_attempt_and_exception(client, monkeypatch, caplog):
    def _raise(date):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backup_mod, "restore_db", _raise)

    with caplog.at_level(logging.INFO, logger="api.backup"):
        resp = client.post("/api/backup/2026-02-02/restore")

    assert resp.status_code == 500

    messages = [r.getMessage() for r in caplog.records]
    assert any("requested" in m and "2026-02-02" in m for m in messages)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "2026-02-02" in errors[0].getMessage()
    assert errors[0].exc_info is not None
