"""The shipped compose file must realise the queue bounds code declares (#188).

An RQ worker takes one job at a time, so a queue's concurrency is the number of
worker processes listening on it — a fact that lives entirely in the deployment
file, while the bounds it has to satisfy are documented in ``src/jobs/queue.py``.
Nothing at runtime spans the two, which is how both workers ended up consuming
``poster`` at ``replicas: 2`` despite the queue being documented as "keep it to
one". These tests are that missing link.

They read ``docker-compose.yml.example`` rather than the real
``docker-compose.yml``, which is gitignored and per-host. The example is the
thing a new deployment is copied from, so it is the one that has to be right.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from src.jobs.queue import ALL_QUEUES, QUEUE_MAX_CONCURRENCY, QUEUE_POSTER

_COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml.example"
_API_SERVICE = "traxjourney"

# Volumes the worker must share with the API. The database is a SQLite file, so
# "sharing" it means sharing the mount; a worker with its own ./db writes to a
# different database and its results silently never appear.
_SHARED_MOUNTS = ("/app/db", "/app/config", "/app/data")


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


@pytest.fixture(scope="module")
def workers(services) -> dict:
    """Service name -> the queue list it consumes, in RQ's dequeue order."""
    found = {}
    for name, spec in services.items():
        command = spec.get("command") or []
        if command and str(command[0]).endswith("worker-entrypoint.sh"):
            found[name] = [str(arg) for arg in command[1:]]
    assert found, "no worker services found — did the entrypoint path change?"
    return found


def _instances(spec: dict) -> int:
    """Processes this service starts. Absent `deploy.replicas` means one."""
    return int(spec.get("deploy", {}).get("replicas", 1))


class TestQueueConcurrency:
    def test_every_queue_has_a_consumer(self, workers):
        """A queue nobody listens on accepts jobs that are never run."""
        consumed = {queue for queues in workers.values() for queue in queues}
        assert consumed == set(ALL_QUEUES)

    def test_no_worker_listens_on_an_unknown_queue(self, workers):
        """Guards a typo: RQ happily creates and blocks on a misspelt queue."""
        for name, queues in workers.items():
            unknown = set(queues) - set(ALL_QUEUES)
            assert not unknown, f"{name} consumes unknown queue(s): {sorted(unknown)}"

    def test_listener_counts_match_the_declared_bounds(self, services, workers):
        """The bug in #188: two replicas on every queue, including `poster`."""
        listeners = dict.fromkeys(ALL_QUEUES, 0)
        for name, queues in workers.items():
            for queue in queues:
                listeners[queue] += _instances(services[name])

        assert listeners == QUEUE_MAX_CONCURRENCY

    def test_only_one_process_can_render_a_poster(self, services, workers):
        """Stated separately from the map because the failure is an OOM.

        Two concurrent A0 renders are what takes a small host down, and the
        whole reason workers run outside the API container.
        """
        renderers = [n for n, queues in workers.items() if QUEUE_POSTER in queues]
        assert len(renderers) == 1, f"expected one poster service, got {renderers}"
        assert _instances(services[renderers[0]]) == 1

    def test_a_dedicated_queue_is_listed_first(self, workers):
        """RQ dequeues in the order given, so a sole consumer must prefer its
        own queue — otherwise a busy shared queue starves it indefinitely."""
        for name, queues in workers.items():
            sole = [q for q in queues
                    if sum(q in other for other in workers.values()) == 1]
            for queue in sole:
                assert queues[0] == queue, (
                    f"{name} is the only consumer of {queue!r} but lists it "
                    f"after {queues[:queues.index(queue)]}")


class TestMemoryLimits:
    """A runaway job must hit a per-container ceiling, not the whole host's
    (issue #209): with no swap on the VPS, an unbounded container was free to
    consume all available RAM, which produced a multi-hour system-wide lockup
    instead of a clean OOM-kill `restart: unless-stopped` could recover from.
    """

    _LIMITED_SERVICES = ("traxjourney", "worker", "worker-poster", "redis")

    @staticmethod
    def _memory_limit_bytes(spec: dict) -> int:
        raw = spec["deploy"]["resources"]["limits"]["memory"]
        units = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000,
                 "ki": 1024, "mi": 1024 ** 2, "gi": 1024 ** 3}
        for suffix in sorted(units, key=len, reverse=True):
            if raw.lower().endswith(suffix):
                return int(raw[: -len(suffix)]) * units[suffix]
        return int(raw)

    def test_every_worker_and_the_api_declare_a_memory_limit(self, services):
        for name in self._LIMITED_SERVICES:
            assert "deploy" in services[name], f"{name} has no memory limit set"
            limit = self._memory_limit_bytes(services[name])
            assert limit > 0, f"{name} has a non-positive memory limit"

    def test_limits_fit_a_single_4gb_host_with_room_to_spare(self, services):
        """Sized for one stack (docs/DEPLOYMENT_VPS.md's 4 GB VPS). Halving
        these is a documented operator step when a second stack shares the
        host — this just guards against the total creeping past what even one
        stack alone should be allowed to take."""
        total = sum(
            self._memory_limit_bytes(services[name])
            for name in self._LIMITED_SERVICES)
        four_gb = 4 * 1024 ** 3
        assert total < four_gb, (
            f"declared memory limits sum to {total} bytes, "
            f"at or above the documented 4 GB host size")


class TestWorkersTrackTheApi:
    def test_workers_run_the_same_image_tag_as_the_api(self, services, workers):
        """They share one image and only the API migrates. A worker on an older
        tag runs stale job code against a schema it does not know about."""
        api_image = services[_API_SERVICE]["image"]
        for name in workers:
            assert services[name]["image"] == api_image

    def test_workers_mount_the_same_host_paths_as_the_api(self, services, workers):
        def mounts(spec):
            return {v.split(":")[1] for v in spec["volumes"] if ":" in v}

        api_mounts = mounts(services[_API_SERVICE])
        for name in workers:
            for target in _SHARED_MOUNTS:
                if target in api_mounts:
                    assert target in mounts(services[name]), (
                        f"{name} does not share {target} with the API")

    def test_workers_declare_the_worker_role(self, services, workers):
        """TRAXJOURNEY_ROLE=worker is what stops a worker running migrations, the
        admin seed and the scheduler (api/router.py). The entrypoint exports it
        too; this keeps the compose file honest about what the service is."""
        for name in workers:
            assert services[name].get("environment", {}).get("TRAXJOURNEY_ROLE") == "worker"
