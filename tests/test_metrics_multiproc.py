"""PROMETHEUS_MULTIPROC_DIR hygiene (issue #437).

With the directory set, every process writes its samples to per-process files
there and ``/metrics`` sums them. The API and the worker containers share the
directory, so:

- file names must be unique across containers, not just across PIDs: each
  container has its own PID namespace, and the API and every worker's parent
  are all PID 1;
- files left by an earlier run must be cleared before anything writes, or dead
  processes' counters and gauges keep being exported;
- clearing must never delete a live process' files.

The wipe relies on ``flock``, so those tests are POSIX-only (CI and the
containers are Linux).
"""
from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils import metrics_multiproc

_ROOT = Path(__file__).resolve().parent.parent

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="flock is POSIX-only; the containers are Linux"
)


def _db_files(path) -> set[str]:
    return {os.path.basename(f) for f in glob.glob(os.path.join(path, "*.db"))}


def _stale(path, *names) -> None:
    for name in names:
        (Path(path) / name).write_bytes(b"\0" * 8)


def _run(code: str, env_value: str | None, cwd) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    if env_value is not None:
        env["PROMETHEUS_MULTIPROC_DIR"] = env_value
    env["PYTHONPATH"] = str(_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=60,
    )


# ── Process identifier ───────────────────────────────────────────────────────

class TestProcessIdentifier:
    def test_names_the_container_as_well_as_the_pid(self):
        """Two containers' PID 1 must not share a file."""
        ident = metrics_multiproc.process_identifier()
        assert ident.endswith(f"-{os.getpid()}")
        assert socket.gethostname().split(".")[0][:8] in ident

    def test_can_name_another_process_in_this_container(self):
        assert metrics_multiproc.process_identifier(4242).endswith("-4242")
        assert (metrics_multiproc.process_identifier(4242)
                != metrics_multiproc.process_identifier(4243))

    def test_has_no_underscore(self):
        """prometheus_client splits ``gauge_<mode>_<id>.db`` on ``_``."""
        assert "_" not in metrics_multiproc.process_identifier()


# ── Clearing at stack start ──────────────────────────────────────────────────

@posix_only
class TestClaim:
    def test_first_writer_clears_files_left_by_old_pids(self, tmp_path):
        _stale(tmp_path, "counter_1.db", "histogram_1.db", "gauge_all_1.db",
               "gauge_livesum_abc-77.db")
        fd = metrics_multiproc.claim_multiproc_dir(str(tmp_path))
        try:
            assert _db_files(tmp_path) == set()
        finally:
            os.close(fd)

    def test_a_live_writer_keeps_everyone_elses_files(self, tmp_path):
        live = metrics_multiproc.claim_multiproc_dir(str(tmp_path))
        try:
            _stale(tmp_path, "counter_live-1.db")
            fd = metrics_multiproc.claim_multiproc_dir(str(tmp_path))
            os.close(fd)
            assert _db_files(tmp_path) == {"counter_live-1.db"}
        finally:
            os.close(live)

    def test_clears_again_once_every_writer_is_gone(self, tmp_path):
        os.close(metrics_multiproc.claim_multiproc_dir(str(tmp_path)))
        _stale(tmp_path, "counter_gone-1.db")
        os.close(metrics_multiproc.claim_multiproc_dir(str(tmp_path)))
        assert _db_files(tmp_path) == set()

    def test_creates_a_missing_directory(self, tmp_path):
        target = tmp_path / "metrics"
        os.close(metrics_multiproc.claim_multiproc_dir(str(target)))
        assert target.is_dir()


@posix_only
def test_a_fresh_process_leaves_no_file_from_old_pids(tmp_path):
    """End to end, in a real process: importing the metrics module is the first
    write, so the wipe has to happen inside that import."""
    stale = {"counter_1.db", "histogram_1.db", "gauge_all_1.db", "counter_old-9.db"}
    _stale(tmp_path, *stale)
    result = _run(
        "import os\n"
        "from src.utils import metrics as m\n"
        "from src.utils.metrics_multiproc import process_identifier\n"
        "m.STALE_WRITES.inc()\n"
        "print(process_identifier())\n",
        str(tmp_path), tmp_path,
    )
    assert result.returncode == 0, result.stderr
    ident = result.stdout.strip()
    files = _db_files(tmp_path)
    assert not files & stale
    assert files and all(f.endswith(f"_{ident}.db") for f in files), files

    # The aggregator can still parse the new names and sees the sample.
    from prometheus_client import CollectorRegistry, generate_latest, multiprocess
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
    assert b"traxjourney_stale_writes_total 1.0" in generate_latest(registry)


@posix_only
def test_a_process_starting_beside_a_live_one_keeps_its_files(tmp_path):
    """The API and a work-horse start and stop independently: one starting
    must never delete the other's files while it runs."""
    env = {k: v for k, v in os.environ.items()}
    env.update(PROMETHEUS_MULTIPROC_DIR=str(tmp_path), PYTHONPATH=str(_ROOT))
    live = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\n"
         "from src.utils import metrics as m\n"
         "from src.utils.metrics_multiproc import process_identifier\n"
         "m.STALE_WRITES.inc()\n"
         "print(process_identifier(), flush=True)\n"
         "sys.stdin.read()\n"],
        cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        live_ident = live.stdout.readline().strip()
        assert live_ident, "live writer did not start"
        code = "from src.utils import metrics as m\nm.STALE_WRITES.inc()\n"
        assert _run(code, str(tmp_path), tmp_path).returncode == 0

        assert f"counter_{live_ident}.db" in _db_files(tmp_path)
        from prometheus_client import CollectorRegistry, generate_latest, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
        assert b"traxjourney_stale_writes_total 2.0" in generate_latest(registry)
    finally:
        live.communicate("", timeout=30)

    # Every writer gone: the next start is a stack start and clears it all.
    assert _run(code, str(tmp_path), tmp_path).returncode == 0
    assert not any(live_ident in f for f in _db_files(tmp_path))


def test_an_empty_setting_writes_no_files(tmp_path):
    """``.env.example`` ships ``PROMETHEUS_MULTIPROC_DIR=`` (empty), which the
    compose ``env_file`` passes through as set-but-empty. prometheus_client
    then enables multiprocess mode on presence alone and writes every
    process' files into its working directory, one set per RQ job, forever.
    ``/metrics`` already treats empty as unset; the writers must agree."""
    result = _run(
        "from src.utils import metrics as m\nm.STALE_WRITES.inc()\n",
        "", tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert _db_files(tmp_path) == set()


# ── Work-horse exit ──────────────────────────────────────────────────────────

class TestForgetProcess:
    def test_removes_only_that_process_live_gauge_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        dead = metrics_multiproc.process_identifier(4242)
        alive = metrics_multiproc.process_identifier(4243)
        _stale(tmp_path, f"gauge_livesum_{dead}.db", f"gauge_livesum_{alive}.db",
               f"counter_{dead}.db")
        metrics_multiproc.forget_process(4242)
        # Counters of a dead process stay until the next stack start: dropping
        # them would make the summed counter go backwards.
        assert _db_files(tmp_path) == {f"gauge_livesum_{alive}.db", f"counter_{dead}.db"}

    def test_is_a_no_op_without_the_directory(self, monkeypatch):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        metrics_multiproc.forget_process(4242)  # must not raise


# ── Gauges from more than one process ────────────────────────────────────────
# A container replaced without a full stop (``pull && up -d``) leaves its files
# behind while a live writer keeps the directory from being cleared. A gauge in
# the default ``all`` mode then exports one series per process, the dead one
# frozen: 25 h later ``time() - job_last_success{job_name="daily_backup"}``
# matches that series and raises a false "backup silently stopped".

def _gauge_file(path, gauge, ident, labelvalues, value, timestamp):
    from prometheus_client.mmap_dict import MmapedDict, mmap_key

    f = MmapedDict(os.path.join(path, f"gauge_{gauge._multiprocess_mode}_{ident}.db"))
    try:
        key = mmap_key(gauge._name, gauge._name, list(gauge._labelnames),
                       list(labelvalues), gauge._documentation)
        f.write_value(key, value, timestamp)
    finally:
        f.close()


def _aggregated(path, name):
    from prometheus_client import CollectorRegistry, multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(path))
    return [s for m in registry.collect() for s in m.samples if s.name == name]


@pytest.mark.parametrize("attr, labelvalues, old, new, expected", [
    # A last-success timestamp only moves forward: the largest is the answer.
    ("JOB_LAST_SUCCESS", ("daily_backup",), (1_000.0, 0.0), (90_000.0, 0.0), 90_000.0),
    # The same constant in every process that imports the Strava client.
    ("STRAVA_RATE_LIMIT_CAPACITY", ("daily",), (1000.0, 0.0), (1000.0, 0.0), 1000.0),
    # A measurement: the latest one wins, even when it is the smaller.
    ("PREPARED_GEOMETRY_BACKLOG", (), (50.0, 100.0), (3.0, 200.0), 3.0),
])
def test_a_gauge_from_a_dead_and_a_live_process_is_one_series(
        tmp_path, attr, labelvalues, old, new, expected):
    from src.utils import metrics as app_metrics

    gauge = getattr(app_metrics, attr)
    _gauge_file(tmp_path, gauge, "oldcontainer-1", labelvalues, *old)
    _gauge_file(tmp_path, gauge, "newcontainer-1", labelvalues, *new)

    samples = _aggregated(tmp_path, gauge._name)
    assert len(samples) == 1, samples
    assert "pid" not in samples[0].labels
    assert samples[0].value == expected


@pytest.mark.parametrize("other_gauge_uses_all", [False, True])
@pytest.mark.parametrize("all_last", [False, True])
def test_gauge_files_left_by_an_older_mode_are_not_read(
        tmp_path, monkeypatch, all_last, other_gauge_uses_all):
    """An in-place upgrade (``pull && up -d``, no ``down``) keeps the old
    containers' ``gauge_all_*`` files while live writers stop the directory
    from being cleared. prometheus_client takes a gauge's combine mode from
    whichever file it reads last, so an old ``all`` file read after the new
    ``max`` one brings back the frozen per-pid series, and with it the false
    "backup silently stopped" alarm. Checked with the files in either order,
    since the glob order is the filesystem's.

    The expected mode is per gauge: another gauge still using ``all`` must not
    make an ``all`` file readable for this one."""
    import api.metrics as metrics_endpoint
    from prometheus_client import generate_latest
    from src.utils import metrics as app_metrics

    gauge = app_metrics.JOB_LAST_SUCCESS
    old = tmp_path / "gauge_all_oldcontainer-1.db"
    new = tmp_path / f"gauge_{gauge._multiprocess_mode}_newcontainer-1.db"
    for path, value in ((old, 1_000.0), (new, 90_000.0)):
        from prometheus_client.mmap_dict import MmapedDict, mmap_key
        f = MmapedDict(str(path))
        f.write_value(mmap_key(gauge._name, gauge._name, list(gauge._labelnames),
                               ["daily_backup"], gauge._documentation), value, 0.0)
        f.close()

    real_glob = glob.glob

    def ordered_glob(pattern, *args, **kwargs):
        found = real_glob(pattern, *args, **kwargs)
        return sorted(found, key=lambda f: ("gauge_all_" in f) == all_last)

    monkeypatch.setattr(glob, "glob", ordered_glob)
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    from prometheus_client import REGISTRY, Gauge
    other = None
    if other_gauge_uses_all:
        other = Gauge("traxjourney_test_other_all_mode", "Probe.", multiprocess_mode="all")
    try:
        text = generate_latest(metrics_endpoint._registry()).decode()
    finally:
        if other is not None:
            REGISTRY.unregister(other)
    lines = [l for l in text.splitlines() if l.startswith(gauge._name + "{")]
    assert lines == [f'{gauge._name}{{job_name="daily_backup"}} 90000.0'], lines
