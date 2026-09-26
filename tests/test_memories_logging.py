"""Logging tests for api/memories.py's background photo-download path.

Issue #205 Unit E: `_download_photo_from_url` used to swallow every fetch
failure with a bare `except Exception: return`, leaving the memory without a
photo and zero trace anywhere. These tests confirm the failure still leaves
the entry unchanged, but now also produces a log line naming the memory,
its project, and the source URL.
"""
from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from models.project_db import DBMemory, DBProject
from models.user import UserInfo


@pytest.fixture
def memory_db(monkeypatch):
    """In-memory DB with one user, one project, one memory (no photos)."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)

        project = DBProject(user_info_id=user.id, name="My Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)

        memory = DBMemory(project_id=project.id, date="2025-06-01", photos_json="[]")
        sess.add(memory)
        sess.commit()
        sess.refresh(memory)

        return test_engine, user.id, project.id, memory.id


def test_failed_fetch_leaves_memory_unchanged_and_logs(memory_db, monkeypatch, caplog):
    test_engine, user_id, project_id, memory_id = memory_db

    import api.memories as memories_mod

    def _raise(*args, **kwargs):
        raise ConnectionError("boom")

    monkeypatch.setattr(memories_mod, "fetch_bytes", _raise)

    with caplog.at_level(logging.WARNING, logger="api.memories"):
        memories_mod._download_photo_from_url(
            memory_id, "http://bad.example/photo.jpg", str(user_id), project_id
        )

    # Memory still has no photo — behavior unchanged.
    with Session(test_engine) as sess:
        row = sess.get(DBMemory, memory_id)
        assert json.loads(row.photos_json or "[]") == []

    # But the failure is now visible in the logs, with enough to reproduce it.
    # Scoped to this module's logger — caplog.records is process-wide and can
    # otherwise pick up an unrelated module's import-time warning depending on
    # what else has already imported in this test run.
    records = [r for r in caplog.records
               if r.levelno >= logging.WARNING and r.name == "api.memories"]
    assert len(records) == 1
    record = records[0]
    msg = record.getMessage()
    assert str(memory_id) in msg
    assert str(project_id) in msg
    assert str(user_id) in msg
    assert "http://bad.example/photo.jpg" in msg
    # .exception() captures the traceback.
    assert record.exc_info is not None
