"""The `video` queue: never in-process, never retried, its killed jobs failed.

A trip video render takes minutes, so running it in the API process would starve
every other request (plan D7), and a retry would render — and charge for — the
same video twice. These tests pin the queue plumbing that makes both true, and
the killed-work-horse dispatch a video job will register with.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

import src.jobs.queue as queue_mod
import src.jobs.redis_client as redis_client
import src.jobs.worker as worker_mod
from src.jobs.queue import (
    ALL_QUEUES,
    QUEUE_MAX_CONCURRENCY,
    QUEUE_POSTER,
    QUEUE_RESOLVE,
    QUEUE_VIDEO,
    enqueue,
)

_COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml.example"

_calls: list = []


def _record(*args, **kwargs):
    _calls.append((args, kwargs))


class _FakeQueue:
    """Records what `enqueue` hands RQ instead of talking to a broker."""

    def __init__(self):
        self.enqueued: list[tuple[tuple, dict]] = []

    def enqueue(self, *args, **kwargs):
        self.enqueued.append((args, kwargs))


class _FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


@pytest.fixture(autouse=True)
def _clear():
    _calls.clear()
    yield
    _calls.clear()
    redis_client.reset_redis()


@pytest.fixture
def fake_queue(monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(queue_mod, "get_queue", lambda name: queue)
    return queue


@pytest.fixture
def no_broker(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_client.reset_redis()


@pytest.fixture
def broken_broker(monkeypatch):
    class _Exploding:
        def enqueue(self, *args, **kwargs):
            raise RuntimeError("broker went away")

    monkeypatch.setattr(queue_mod, "get_queue", lambda name: _Exploding())


class TestTheQueue:
    def test_video_is_a_known_queue_with_one_consumer(self):
        assert QUEUE_VIDEO == "video"
        assert QUEUE_VIDEO in ALL_QUEUES
        assert QUEUE_MAX_CONCURRENCY[QUEUE_VIDEO] == 1

    def test_one_compose_service_consumes_video_and_nothing_else(self):
        """Its own service, so a render never holds up the cheap queues and
        the cheap queues' bounds don't change."""
        services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
        consumers = {
            name: [str(a) for a in spec["command"][1:]]
            for name, spec in services.items()
            if QUEUE_VIDEO in [str(a) for a in (spec.get("command") or [])[1:]]
        }
        assert consumers == {"worker-video": [QUEUE_VIDEO]}
        assert services["worker-video"].get("deploy", {}).get("replicas", 1) == 1


class TestEnqueueOptions:
    def test_no_retries_enqueues_without_a_retry(self, fake_queue):
        """RQ's Retry(max=0) raises, which the fallback would swallow and then
        run the job in-process — so zero retries must mean no Retry at all."""
        assert enqueue(QUEUE_VIDEO, _record, 7, max_retries=0) is True

        (args, kwargs), = fake_queue.enqueued
        assert "retry" not in kwargs
        assert args == (queue_mod._run_with_level_refresh, _record, 7)
        assert _calls == []

    def test_retries_are_still_the_default(self, fake_queue):
        """Unchanged for every existing caller (poster included)."""
        from rq import Retry

        enqueue(QUEUE_POSTER, _record, 7)

        (_, kwargs), = fake_queue.enqueued
        assert isinstance(kwargs["retry"], Retry)
        assert kwargs["retry"].max == 3

    def test_job_timeout_is_passed_to_rq_not_to_the_job(self, fake_queue):
        enqueue(QUEUE_VIDEO, _record, 7, max_retries=0, job_timeout=1800)

        (args, kwargs), = fake_queue.enqueued
        assert kwargs["job_timeout"] == 1800
        assert args == (queue_mod._run_with_level_refresh, _record, 7)

    def test_without_job_timeout_the_queue_default_applies(self, fake_queue):
        enqueue(QUEUE_RESOLVE, _record, 7)

        (_, kwargs), = fake_queue.enqueued
        assert "job_timeout" not in kwargs


class TestNoInlineFallback:
    def test_no_broker_returns_false_and_does_not_run(self, no_broker):
        bg = _FakeBackgroundTasks()

        assert enqueue(QUEUE_RESOLVE, _record, 1, background_tasks=bg,
                       allow_inline=False) is False
        assert _calls == []
        assert bg.tasks == []

    def test_a_failing_broker_returns_false_and_does_not_run(self, broken_broker):
        bg = _FakeBackgroundTasks()

        assert enqueue(QUEUE_RESOLVE, _record, 1, background_tasks=bg,
                       allow_inline=False) is False
        assert _calls == []
        assert bg.tasks == []

    def test_the_video_queue_never_runs_inline_even_if_allowed(self, no_broker):
        """The queue's own property, not only the caller's flag (D7)."""
        bg = _FakeBackgroundTasks()

        assert enqueue(QUEUE_VIDEO, _record, 1, background_tasks=bg) is False
        assert enqueue(QUEUE_VIDEO, _record, 1) is False
        assert _calls == []
        assert bg.tasks == []

    def test_other_queues_still_fall_back_by_default(self, no_broker):
        assert enqueue(QUEUE_RESOLVE, _record, 1) is False
        assert _calls == [((1,), {})]


class _FakeJob:
    def __init__(self, args, id="job-1"):
        self.args = args
        self.id = id


class TestKilledHandlerDispatch:
    def test_a_killed_poster_job_is_still_failed_with_its_message(self, monkeypatch):
        import src.poster.poster_job_runner as job_runner_mod

        calls = []
        monkeypatch.setattr(job_runner_mod, "mark_job_interrupted",
                            lambda job_id, reason: calls.append((job_id, reason)))

        worker_mod._work_horse_killed_handler(
            _FakeJob(args=(job_runner_mod.run_poster_job, 42)), 123, 9, None)

        assert calls == [(
            42,
            "The poster generation process was terminated unexpectedly "
            "(likely out of memory) — try a smaller region or fewer "
            "photos, or try again.",
        )]

    def test_an_unknown_function_is_ignored(self, monkeypatch):
        import src.poster.poster_job_runner as job_runner_mod

        calls = []
        monkeypatch.setattr(job_runner_mod, "mark_job_interrupted",
                            lambda job_id, reason: calls.append((job_id, reason)))

        worker_mod._work_horse_killed_handler(_FakeJob(args=(_record, 42)), 123, 9, None)
        worker_mod._work_horse_killed_handler(_FakeJob(args=()), 123, 9, None)

        assert calls == []

    def test_a_registered_entry_is_dispatched_by_dotted_path(self, monkeypatch):
        """What a new job type gets by adding one line to the mapping."""
        interrupted = []
        monkeypatch.setitem(
            worker_mod._INTERRUPT_HANDLERS,
            f"{__name__}._record",
            (f"{__name__}._mark_interrupted", "render killed"))
        monkeypatch.setattr(
            f"{__name__}._mark_interrupted",
            lambda job_id, reason: interrupted.append((job_id, reason)))

        worker_mod._work_horse_killed_handler(_FakeJob(args=(_record, 7)), 123, 9, None)

        assert interrupted == [(7, "render killed")]

    def test_every_registered_path_imports(self):
        """A typo in the mapping would only show up when a job is OOM-killed."""
        for func_path, (mark_path, reason) in worker_mod._INTERRUPT_HANDLERS.items():
            assert callable(worker_mod._import_dotted(func_path))
            assert callable(worker_mod._import_dotted(mark_path))
            assert reason


def _mark_interrupted(job_id, reason):
    raise AssertionError("replaced in the test that registers it")
