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
