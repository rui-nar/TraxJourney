"""An unusable HEIF_DECODE_MEMORY_MIB stops the API and the worker at start
(issue #33, U9 step 1a of docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md).

Without the check, a bad value would surface only on the first HEIC photo,
as that photo's failure. Unset, blank and a positive whole number start.
"""
from __future__ import annotations

import logging

import pytest

import src.jobs.worker as worker_mod
from src.utils.photo_store import HEIF_DECODE_MEMORY_ENV, HeifDecodeConfigError

_BAD = ["0", "-5", "abc", "1.5", "896MiB"]
_GOOD = [None, "", "   ", "896"]


def _set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(HEIF_DECODE_MEMORY_ENV, raising=False)
    else:
        monkeypatch.setenv(HEIF_DECODE_MEMORY_ENV, value)


class TestApi:
    @pytest.mark.parametrize("value", _BAD)
    def test_refuses_to_start(self, monkeypatch, value):
        import api.router as router
        from fastapi.testclient import TestClient

        _set(monkeypatch, value)
        # The worker path: the lifespan checks its settings and then yields.
        monkeypatch.setattr(router, "_IS_API_PROCESS", False)

        with pytest.raises(HeifDecodeConfigError) as caught:
            with TestClient(router.app):
                pass
        message = str(caught.value)
        assert HEIF_DECODE_MEMORY_ENV in message
        assert repr(value) in message
        assert "positive whole number of MiB" in message
        assert f"set {HEIF_DECODE_MEMORY_ENV}=576" in message

    @pytest.mark.parametrize("value", _GOOD)
    def test_starts(self, monkeypatch, value):
        import api.router as router
        from fastapi.testclient import TestClient

        _set(monkeypatch, value)
        monkeypatch.setattr(router, "_IS_API_PROCESS", False)

        with TestClient(router.app) as client:
            assert client.get("/api/version").status_code == 200


class _FakeWorker:
    started = False

    def __init__(self, queues, connection=None, work_horse_killed_handler=None):
        pass

    def work(self, with_scheduler):
        _FakeWorker.started = True


class TestWorker:
    @pytest.fixture(autouse=True)
    def _broker(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://example:6379/0")
        monkeypatch.setattr(worker_mod, "_connect_with_retry", lambda: object())
        monkeypatch.setattr("rq.Worker", _FakeWorker)
        _FakeWorker.started = False

    @pytest.mark.parametrize("value", _BAD)
    def test_refuses_to_start(self, monkeypatch, caplog, value):
        _set(monkeypatch, value)
        connected = []
        monkeypatch.setattr(worker_mod, "_connect_with_retry",
                            lambda: connected.append(1) or object())

        with caplog.at_level(logging.ERROR):
            assert worker_mod.main(["resolve"]) == 1

        assert not _FakeWorker.started and connected == []
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert HEIF_DECODE_MEMORY_ENV in logged
        assert repr(value) in logged
        assert "positive whole number of MiB" in logged
        assert f"set {HEIF_DECODE_MEMORY_ENV}=576" in logged

    @pytest.mark.parametrize("value", _GOOD)
    def test_starts(self, monkeypatch, value):
        _set(monkeypatch, value)

        assert worker_mod.main(["resolve"]) == 0
        assert _FakeWorker.started
