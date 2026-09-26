"""The worker's own Redis-connect retry loop (issue #209).

Before this, a worker that couldn't reach Redis within the client's 2s socket
timeout exited(1) immediately. `restart: unless-stopped` relaunched it a few
seconds later, which — when Redis was merely slow rather than actually down —
burned more CPU/memory and deepened the contention that made it slow, turning
one overloaded host into a self-sustaining crash loop. The worker now retries
in place instead of exiting, which is what these tests guard.
"""
from __future__ import annotations

import pytest

import src.jobs.worker as worker_mod


class _FakeSleep:
    """Records requested delays instead of actually waiting."""

    def __init__(self, stop_after: int | None = None):
        self.delays: list[float] = []
        self._stop_after = stop_after

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self._stop_after is not None and len(self.delays) >= self._stop_after:
            raise _StopTest()


class _StopTest(Exception):
    """Escapes an intentionally-infinite retry loop in a test."""


class TestConnectWithRetry:
    def test_returns_immediately_when_redis_is_already_up(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(worker_mod, "get_redis", lambda: sentinel)
        sleep = _FakeSleep()

        assert worker_mod._connect_with_retry(sleep=sleep) is sentinel
        assert sleep.delays == []

    def test_retries_with_backoff_until_redis_answers(self, monkeypatch):
        sentinel = object()
        attempts = iter([None, None, sentinel])
        monkeypatch.setattr(worker_mod, "get_redis", lambda: next(attempts))
        reset_calls = []
        monkeypatch.setattr(worker_mod, "reset_redis", lambda: reset_calls.append(1))
        sleep = _FakeSleep()

        assert worker_mod._connect_with_retry(sleep=sleep) is sentinel
        assert sleep.delays == [2, 5]
        assert len(reset_calls) == 2

    def test_backoff_caps_instead_of_growing_unbounded(self, monkeypatch):
        monkeypatch.setattr(worker_mod, "get_redis", lambda: None)
        monkeypatch.setattr(worker_mod, "reset_redis", lambda: None)
        sleep = _FakeSleep(stop_after=8)

        with pytest.raises(_StopTest):
            worker_mod._connect_with_retry(sleep=sleep)

        assert sleep.delays == [2, 5, 10, 30, 60, 60, 60, 60]


class TestMain:
    def test_fails_fast_when_redis_url_is_unset(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)

        def _must_not_be_called():
            raise AssertionError("must not probe Redis when no URL is configured")

        monkeypatch.setattr(worker_mod, "_connect_with_retry", _must_not_be_called)

        assert worker_mod.main([]) == 1

    def test_starts_an_rq_worker_once_connected(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://fake")
        sentinel_client = object()
        monkeypatch.setattr(
            worker_mod, "_connect_with_retry", lambda: sentinel_client)

        started = {}

        class _FakeWorker:
            def __init__(self, queues, connection, work_horse_killed_handler=None):
                started["queues"] = queues
                started["connection"] = connection
                started["work_horse_killed_handler"] = work_horse_killed_handler

            def work(self, with_scheduler):
                started["with_scheduler"] = with_scheduler

        monkeypatch.setattr("rq.Worker", _FakeWorker)

        assert worker_mod.main(["resolve"]) == 0
        assert started == {
            "queues": ["resolve"],
            "connection": sentinel_client,
            "with_scheduler": False,
            "work_horse_killed_handler": worker_mod._work_horse_killed_handler,
        }


class TestWorkHorseKilledHandler:
    """A poster render is memory-heavy enough to get its work-horse
    SIGKILLed (OOM) outright, with no Python exception for run_poster_job's
    own try/except to catch (issue #14 follow-up). This handler is the fast
    path that notices anyway, straight from the still-alive parent worker."""

    class _FakeJob:
        def __init__(self, args, id="job-1"):
            self.args = args
            self.id = id

    def test_a_killed_poster_job_is_marked_interrupted(self, monkeypatch):
        import src.poster.poster_job_runner as job_runner_mod

        calls = []
        monkeypatch.setattr(
            job_runner_mod, "mark_job_interrupted",
            lambda job_id, reason: calls.append((job_id, reason)),
        )

        job = self._FakeJob(args=(job_runner_mod.run_poster_job, 42))
        worker_mod._work_horse_killed_handler(job, 123, 9, None)

        assert len(calls) == 1
        job_id, reason = calls[0]
        assert job_id == 42
        assert "memory" in reason.lower()

    def test_a_killed_non_poster_job_is_ignored(self, monkeypatch):
        import src.poster.poster_job_runner as job_runner_mod

        calls = []
        monkeypatch.setattr(
            job_runner_mod, "mark_job_interrupted",
            lambda job_id, reason: calls.append((job_id, reason)),
        )

        def _some_other_job(x):
            return x

        job = self._FakeJob(args=(_some_other_job, 42))
        worker_mod._work_horse_killed_handler(job, 123, 9, None)

        assert calls == []

    def test_a_broken_handler_does_not_raise(self, monkeypatch):
        """Must never take the still-running worker process down with it."""
        import src.poster.poster_job_runner as job_runner_mod

        def _boom(job_id, reason):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(job_runner_mod, "mark_job_interrupted", _boom)

        job = self._FakeJob(args=(job_runner_mod.run_poster_job, 42))
        worker_mod._work_horse_killed_handler(job, 123, 9, None)  # must not raise


class TestWorkHorseMetricFiles:
    """Every RQ job runs in a forked work-horse with its own PID, so its own
    metric files (issue #437). Once it exits, the parent tells prometheus_client
    so a live-mode gauge stops counting a process that no longer exists."""

    class _FakeRqWorker:
        horse_pid = 4242

        def __init__(self, fail=False):
            self._fail = fail

        def monitor_work_horse(self, job, queue):
            self.horse_pid = 0  # rq clears it once the horse has exited
            if self._fail:
                raise RuntimeError("monitor blew up")

    def _worker(self, **kw):
        cls = type("W", (worker_mod._ForgetsWorkHorses, self._FakeRqWorker), {})
        return cls(**kw)

    def test_forgets_the_work_horse_once_it_exits(self, monkeypatch):
        forgotten = []
        monkeypatch.setattr(worker_mod, "forget_process", forgotten.append)

        self._worker().monitor_work_horse(None, None)

        assert forgotten == [4242]

    def test_forgets_it_even_when_monitoring_raises(self, monkeypatch):
        forgotten = []
        monkeypatch.setattr(worker_mod, "forget_process", forgotten.append)

        with pytest.raises(RuntimeError):
            self._worker(fail=True).monitor_work_horse(None, None)

        assert forgotten == [4242]

    def test_rq_still_has_the_hooks_it_relies_on(self):
        """An rq upgrade renaming these would silently skip the cleanup."""
        from rq import Worker

        assert callable(Worker.monitor_work_horse)
        assert isinstance(Worker.horse_pid, property)

    def test_main_runs_the_forgetting_worker(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://fake")
        monkeypatch.setattr(worker_mod, "_connect_with_retry", lambda: object())
        built = []

        class _FakeWorker:
            def __init__(self, *args, **kwargs):
                built.append(type(self))

            def work(self, with_scheduler):
                pass

        monkeypatch.setattr("rq.Worker", _FakeWorker)

        assert worker_mod.main(["resolve"]) == 0
        assert issubclass(built[0], worker_mod._ForgetsWorkHorses)
        assert issubclass(built[0], _FakeWorker)
