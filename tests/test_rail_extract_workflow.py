"""Where the rail extracts are built, and what must never leave the runner (#345).

The raw Europe extract is 34.9 GB and the VPS has 40 GB in total, prod and val
included. So the whole pipeline rests on one invariant — **the raw file exists
only on a throwaway CI runner** — and that invariant lives in a workflow file
and two ignore files, where nothing else would ever check it. A single
``COPY . .`` in the Dockerfile is all it takes for a stray extract in the
working tree to become part of an image.

The other half is coverage. Phase 0 decided that adding a country is a config
change plus a rebuild, never a code change, so the matrix has to be read from
``config/rail_regions.yml`` at run time rather than listed in the workflow.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "rail-extract.yml"
FIXTURE = ROOT / "tests" / "fixtures" / "rail_mannheim.osm.pbf"

_spec = importlib.util.spec_from_file_location(
    "build_rail_extract_workflow", ROOT / "scripts" / "build_rail_extract.py"
)
rail = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rail
_spec.loader.exec_module(rail)

# Aggregates Geofabrik also publishes under europe/. Each one duplicates
# countries the config already lists, which would double the build and leave
# phase 3 with two regions claiming the same coordinate.
AGGREGATE_REGIONS = {
    "europe/alps", "europe/dach", "europe/britain-and-ireland",
    "europe/united-kingdom",
}


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow) -> dict:
    return workflow["jobs"]


def _steps_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


# ---------------------------------------------------------------------------
# Coverage is configuration
# ---------------------------------------------------------------------------

def test_regions_are_geofabrik_paths():
    regions = rail.load_regions()
    assert regions
    assert all(region.startswith("europe/") for region in regions)
    # The two the filter was measured against in the spike (issue #345).
    assert {"europe/denmark", "europe/germany"} <= set(regions)


def test_no_aggregate_regions():
    assert not AGGREGATE_REGIONS & set(rail.load_regions())


def test_colliding_file_names_are_rejected(tmp_path):
    """The artifact is named after the last path component, so two regions
    sharing one would have the second overwrite the first in the release."""
    config = tmp_path / "regions.yml"
    config.write_text("regions: [europe/georgia, asia/georgia]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="collide"):
        rail.load_regions(config)


def test_an_empty_config_is_rejected(tmp_path):
    config = tmp_path / "regions.yml"
    config.write_text("regions: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no regions"):
        rail.load_regions(config)


def test_the_matrix_is_read_from_the_config_not_the_workflow(jobs):
    """Adding a country must not require editing the workflow."""
    matrix = jobs["build"]["strategy"]["matrix"]["region"]
    assert "needs.plan.outputs.regions" in matrix
    assert "build_rail_extract.py regions --json" in _steps_text(jobs["plan"])


def test_the_plan_job_runs_with_only_what_it_installs(tmp_path):
    """The matrix command must work in the plan job's environment, which is
    PyYAML and nothing else.

    It did not. `osmium` and `requests` were imported at module scope, so the
    step that prints the matrix died with ModuleNotFoundError before printing
    anything — the plan job failed on every scheduled run and `build` never
    started. Nothing caught it, because every test here imports the module in a
    developer's environment where both are installed and asserts on the text of
    the workflow file rather than on the command working.

    So: run it for real, with the heavy pair made unimportable. PYTHONPATH takes
    precedence over site-packages, so a stub module that raises stands in for
    "not installed" without touching the environment the suite runs in.
    """
    for module in ("osmium", "requests"):
        (tmp_path / f"{module}.py").write_text(
            f'raise ImportError("No module named {module!r}")\n', encoding="utf-8"
        )
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, "scripts/build_rail_extract.py", "regions", "--json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == rail.load_regions()


# ---------------------------------------------------------------------------
# Nothing raw leaves the runner
# ---------------------------------------------------------------------------

def test_the_raw_extract_is_downloaded_outside_the_checkout(jobs):
    """A work dir inside the workspace would put a 4.83 GB file where
    upload-artifact, docker build and git all look.

    Only the workflow can say where the runner puts it, so this one is on the
    file. That the script then *deletes* it is asserted on the script's own
    behaviour, in test_rail_extract.py.
    """
    build = _steps_text(jobs["build"])
    assert "--work-dir \"$RUNNER_TEMP" in build
    assert "--out-dir dist/rail" in build


def test_only_filtered_artifacts_are_uploaded(jobs):
    """The release gets the ~10 MB result and the manifest, and nothing else."""
    upload = [step for step in jobs["build"]["steps"]
              if step.get("uses", "").startswith("actions/upload-artifact")]
    assert [step["with"]["path"] for step in upload] == ["dist/rail/"]

    publish = _steps_text(jobs["publish"])
    # One file per layer; rail's name is the one every earlier release used.
    for layer in rail.LAYERS:
        assert f"dist/rail/*-{layer}.osm.pbf" in publish
    assert "dist/rail/manifest.json" in publish


def test_no_raw_extract_is_tracked_in_the_repo():
    """The image is built with `COPY . .`, so tracked is shipped. A raw country
    extract is 0.5-4.8 GB; every fixture the suite needs is a filtered cut of
    one and fits in a few hundred KB."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.pbf", "*.osm.bz2", "*.osm"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    paths = sorted(p for p in tracked.split("\0") if p)
    assert paths, "the fixtures are gone, not the guard"
    for path in paths:
        assert path.startswith("tests/fixtures/"), f"{path} is not a fixture"
        assert (ROOT / path).stat().st_size < 1_000_000, f"{path} is not filtered"


def test_the_fixture_stays_small():
    """It is a test input, not a data set; keep it reviewable in a diff."""
    assert FIXTURE.stat().st_size < 1_000_000


def test_extracts_are_gitignored_except_the_fixture():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.osm.pbf" in ignore
    assert "!tests/fixtures/*.osm.pbf" in ignore


def test_extracts_cannot_reach_an_image():
    """Belt and braces with .gitignore: an untracked extract in the working
    tree is still in `docker build`'s context."""
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "*.osm.pbf" in ignore
    assert "dist/" in ignore


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def test_the_build_is_scheduled_and_manually_triggerable(workflow):
    """Monthly is ample — rail alignments change over years — but a region that
    fails has to be re-runnable on the spot."""
    # PyYAML reads the unquoted key `on:` as the boolean True.
    triggers = workflow[True]
    assert triggers["schedule"], "no schedule: the extracts would never refresh"
    assert "workflow_dispatch" in triggers
    assert "regions" in triggers["workflow_dispatch"]["inputs"]


def test_one_build_at_a_time(workflow):
    """Two runs would race on the same release tag."""
    assert workflow["concurrency"]["group"] == "rail-extract"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_manifest_is_verified_before_it_is_published(jobs):
    """The artifacts cross a job boundary; a truncated one must fail here."""
    publish = _steps_text(jobs["publish"])
    assert publish.index("build_rail_extract.py manifest") < \
        publish.index("gh release upload")


def test_the_publish_job_checks_coverage_against_the_planned_matrix(jobs, workflow):
    """The script refuses an incomplete manifest (test_rail_extract.py); this is
    the wiring that makes the publish job actually ask it to.

    Both arguments matter and neither is decoration: without `--base` a
    one-region dispatch republishes a manifest that disowns the other 48, and
    without `--expect` a batch of transient Geofabrik failures publishes a
    partial manifest as the current release. `force_publish` is the way past it
    on purpose rather than by accident.
    """
    manifest_step = _steps_text(jobs["publish"])
    assert "--base released/manifest.json" in manifest_step
    assert "--expect \"$EXPECTED\"" in manifest_step
    assert jobs["publish"]["needs"] == ["plan", "build"], \
        "the expected coverage is the plan job's matrix"
    assert "force_publish" in workflow[True]["workflow_dispatch"]["inputs"]
    assert "--force" in manifest_step


def test_a_subset_rebuild_updates_the_release_it_patches(jobs):
    """A dispatch of one region must not open a new dated release: the other 48
    regions exist only as the previous release's assets, so a new tag would hold
    Denmark alone and "latest rail-data-*" would resolve to it."""
    publish = _steps_text(jobs["publish"])
    assert "gh release list" in publish
    assert "startswith(\"rail-data-\")" in publish


def test_only_the_publish_job_can_write(workflow, jobs):
    """`publish` writes releases, `notify` writes issues, and no job holds
    both: the job that runs on every outcome is the last one that should be
    able to touch the data."""
    assert workflow["permissions"] == {"contents": "read"}
    writers = {
        name: {scope for scope, level in job.get("permissions", {}).items()
               if level == "write"}
        for name, job in jobs.items()
    }
    assert {name for name, scopes in writers.items() if "contents" in scopes} \
        == {"publish"}
    assert {name for name, scopes in writers.items() if "issues" in scopes} \
        == {"notify"}
    assert not any({"contents", "issues"} <= scopes for scopes in writers.values())
    assert "permissions" not in jobs["build"]


# ---------------------------------------------------------------------------
# A failed run is reported where someone will see it
# ---------------------------------------------------------------------------

def test_notify_runs_after_every_job_on_every_outcome(jobs):
    """It must see the result of every other job, and run when they fail —
    which is exactly when a plain `needs` would skip it."""
    notify = jobs["notify"]
    assert set(notify["needs"]) == set(jobs) - {"notify"}
    assert notify["if"] == "always()"
    assert notify["permissions"] == {"issues": "write"}
    assert not any(step.get("uses", "").startswith("actions/checkout")
                   for step in notify["steps"])


def test_both_branches_use_the_rail_data_label(jobs):
    notify = jobs["notify"]
    failure = _step(notify, "Report the failure")
    success = _step(notify, "Close the failure report")
    assert "failure" in failure["if"]
    assert all(f"needs.{job}.result == 'success'" in success["if"]
               for job in notify["needs"])
    assert "--label rail-data" in failure["run"]
    assert "--label rail-data" in success["run"]


def test_the_tag_comes_from_the_publish_job(jobs):
    """Recomputing it in notify would need `contents` and could disagree with
    the release that was actually patched."""
    assert jobs["publish"]["outputs"]["tag"] == "${{ steps.release.outputs.tag }}"
    env = _step(jobs["notify"], "Close the failure report")["env"]
    assert env["TAG"] == "${{ needs.publish.outputs.tag }}"


# ---------------------------------------------------------------------------
# A subset rebuild must never publish a manifest holding only what it built
# ---------------------------------------------------------------------------

def _working_bash() -> str | None:
    """A POSIX shell that actually runs.

    `shutil.which("bash")` is not enough on Windows: WSL installs a `bash` shim
    that fails with `execvpe(/bin/bash) failed` when no distribution is
    installed, which would have let this test "pass" on the wrong exit code.

    Git Bash goes first, because a *working* WSL is no better: Windows
    resolves a bare `bash` to System32's WSL launcher ahead of PATH, and WSL
    does not forward the environment (`TAG`, `REQUESTED`, ...) these tests
    drive the scripts with, so every case ran as if they were unset.
    """
    for candidate in ("C:/Program Files/Git/bin/bash.exe", "bash"):
        if shutil.which(candidate) is None and not Path(candidate).exists():
            continue
        try:
            if subprocess.run([candidate, "-c", "exit 7"],
                              capture_output=True).returncode == 7:
                return candidate
        except OSError:
            continue
    return None


BASH = _working_bash()


def _step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize(
    "requested, release_exists, expected_exit",
    [
        ("europe/denmark", True, 1),
        ("europe/denmark", False, 0),
        ("", True, 0),
    ],
    ids=["subset of an existing release refuses",
         "subset with no release yet bootstraps",
         "full run proceeds"],
)
def test_a_subset_rebuild_stops_only_when_it_would_disown_regions(
    jobs, tmp_path, requested, release_exists, expected_exit
):
    """The merge that fixes the one-region-manifest bug fails *open*.

    `--base` ignores a file that is not there, which is right for the genuine
    first run and wrong for a subset patch: a transient `gh` error, a
    rate-limited token or a release whose manifest asset is missing leaves the
    merge with nothing to merge into, and the run then clobbers a 49-region
    manifest with the one region it built — on a release that still holds all
    49 .pbf assets. `--expect` cannot catch it, because on a dispatch it *is*
    the subset that was requested.

    But refusing on a *missing base* alone also refused the bootstrap, which
    the first real run of this pipeline hit: dispatching one country when no
    `rail-data-*` release exists is not clobbering anything, it is seeding
    coverage a country at a time. So the guard turns on whether there is a
    release to disown regions from, and these three cases are the whole of it.

    Run for real against a fake `gh`, because this is shell inside YAML and
    asserting on its text is how the bug it fixes got shipped.
    """
    script = _step(jobs["publish"], "Fetch the manifest being updated")["run"]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # `release view` decides whether the release exists; `release download`
    # always fails, which is the "base could not be fetched" half.
    verdict = 0 if release_exists else 1
    (fake_bin / "gh").write_text(
        chr(10).join([
            "#!/bin/sh",
            f'if [ "$1 $2" = "release view" ]; then exit {verdict}; fi',
            "exit 1",
            "",
        ]),
        encoding="utf-8",
    )
    (fake_bin / "gh").chmod(0o755)

    result = subprocess.run(
        [BASH, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
             "TAG": "rail-data-2026-08-02", "REQUESTED": requested},
        capture_output=True, text=True,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr


def _run_notify_step(tmp_path, name, jobs, open_issues, env):
    """Run one notify step for real against a fake `gh` and `date`, and return
    (exit code, output, every gh invocation)."""
    script = _step(jobs["notify"], name)["run"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "gh.log"
    # `--jq` is real gh's job; the fake prints what the filter would: the first
    # open issue for `.[0]`, all of them otherwise.
    (fake_bin / "gh").write_text(
        chr(10).join([
            "#!/bin/sh",
            f'printf "%s\n" "$*" >> "{log.as_posix()}"',
            'if [ "$1 $2" = "issue list" ]; then',
            '  case "$*" in',
            '    *".[0].number"*) for n in $FAKE_OPEN; do echo "$n"; break; done ;;',
            '    *) for n in $FAKE_OPEN; do echo "$n"; done ;;',
            "  esac",
            "fi",
            "exit 0",
            "",
        ]),
        # LF on every platform: a CRLF shebang is `/bin/sh\r`, which no shell
        # can execute.
        encoding="utf-8", newline="\n",
    )
    (fake_bin / "date").write_text("#!/bin/sh\necho 2026-10\n",
                                   encoding="utf-8", newline="\n")
    for tool in ("gh", "date"):
        (fake_bin / tool).chmod(0o755)

    result = subprocess.run(
        [BASH, "-e", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
             "RUN_URL": "https://example.test/runs/42",
             "FAKE_OPEN": " ".join(open_issues), **env},
        capture_output=True, text=True,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result.returncode, result.stdout + result.stderr, calls


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize(
    "open_issues, expected",
    [([], "issue create --title Rail extract failed --label rail-data"),
     (["17", "23"], "issue comment 17 ")],
    ids=["no open issue creates one", "an open issue is commented on"],
)
def test_a_failure_files_one_issue(jobs, tmp_path, open_issues, expected):
    code, output, calls = _run_notify_step(
        tmp_path, "Report the failure", jobs, open_issues,
        {"PLAN": "success", "BUILD": "failure", "PUBLISH": "failure"},
    )
    assert code == 0, output
    assert expected in calls
    assert calls.count("issue create") + calls.count("issue comment") == 1
    assert "https://example.test/runs/42 failed in: build, publish" in calls


SUBSET_WARNING = "subset run patched rail-data-2026-09-02; no rail-data release exists for 2026-10"


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize(
    "requested, tag, warns",
    [("europe/denmark", "rail-data-2026-09-02", True),
     ("europe/denmark", "rail-data-2026-10-02", False),
     ("", "rail-data-2026-09-02", False)],
    ids=["subset of an old tag warns", "subset of this month's tag is quiet",
         "full run is quiet"],
)
def test_a_success_closes_every_open_issue(jobs, tmp_path, requested, tag, warns):
    """Guard R1-5: a green subset run that patched last month's release must
    not read as this month's data being out."""
    code, output, calls = _run_notify_step(
        tmp_path, "Close the failure report", jobs, ["17", "23"],
        {"TAG": tag, "REQUESTED": requested},
    )
    assert code == 0, output
    assert "issue close 17 " in calls and "issue close 23 " in calls
    assert "https://example.test/runs/42 succeeded." in calls
    if warns:
        assert f"::warning::{SUBSET_WARNING}" in output
        assert SUBSET_WARNING in calls
    else:
        assert "::warning::" not in output
        assert "no rail-data release exists" not in calls


# ---------------------------------------------------------------------------
# The build job's size guard, per layer
# ---------------------------------------------------------------------------

GUARD_STEP = "Guard — nothing raw leaves this job"
MB = 1 << 20


def _run_guard(tmp_path, jobs, sizes: dict[str, int]):
    """Run the build job's guard for real over files of *sizes* (in MB).

    The files are sized with ``truncate``, so a 301 MB file costs nothing to
    make, and ``find -size`` reads the size, not the content. Every extract
    gets its entry file beside it, as the build writes them.
    """
    script = _step(jobs["build"], GUARD_STEP)["run"]
    dist = tmp_path / "dist" / "rail"
    dist.mkdir(parents=True)
    for name, size in sizes.items():
        with (dist / name).open("wb") as handle:
            handle.truncate(size)
        if name.endswith(".osm.pbf"):
            (dist / name.replace(".osm.pbf", ".entry.json")).write_text("{}")
    runner_temp = tmp_path / "runner-temp"
    (runner_temp / "rail-work").mkdir(parents=True)
    result = subprocess.run(
        [BASH, "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": runner_temp.as_posix()},
        capture_output=True, text=True,
    )
    left = sorted(path.name for path in dist.iterdir())
    for path in dist.iterdir():
        path.unlink()
    return result.returncode, result.stdout + result.stderr, left


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize("name, size, code, kept", [
    ("germany-rail.osm.pbf", 99 * MB, 0, True),
    ("germany-rail.osm.pbf", 101 * MB, 1, True),
    ("germany-ferry.osm.pbf", 19 * MB, 0, True),
    ("germany-ferry.osm.pbf", 21 * MB, 0, False),
    # R1-3: under one 100 MB ceiling, Germany's projected 125 MB bus layer
    # failed the job, and took the region's rail with it.
    ("germany-bus.osm.pbf", 125 * MB, 0, True),
    ("germany-bus.osm.pbf", 299 * MB, 0, True),
    ("germany-bus.osm.pbf", 301 * MB, 0, False),
    # Anything that is not a layer's extract keeps rail's ceiling and its stop.
    ("germany-source.osm.pbf", 101 * MB, 1, True),
], ids=["rail under", "rail over stops", "ferry under", "ferry over drops",
        "germany bus passes", "bus under", "bus over drops",
        "anything else over stops"])
def test_the_guard_has_a_ceiling_per_layer(jobs, tmp_path, name, size, code, kept):
    """Rail 100 MB, ferry 20 MB, bus 300 MB (R1-3). Over its ceiling, rail
    stops the job; ferry or bus is deleted with its entry, so nothing raw
    leaves and the region's rail still publishes — the manifest step then
    warns that the layer is missing."""
    sizes = {name: size}
    if not name.startswith("germany-rail"):
        sizes["germany-rail.osm.pbf"] = 5 * MB
    exit_code, output, left = _run_guard(tmp_path, jobs, sizes)

    assert exit_code == code, output
    entry = name.replace(".osm.pbf", ".entry.json")
    assert (name in left and entry in left) is kept, (left, output)
    assert "germany-rail.osm.pbf" in left
    if not kept:
        assert f"::warning::dist/rail/{name} is over" in output
    if code:
        assert f"::error::dist/rail/{name} is over 100 MB" in output


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
def test_the_guard_still_refuses_a_raw_extract_left_behind(jobs, tmp_path):
    script = _step(jobs["build"], GUARD_STEP)["run"]
    (tmp_path / "dist" / "rail").mkdir(parents=True)
    work = tmp_path / "runner-temp" / "rail-work"
    work.mkdir(parents=True)
    (work / "germany-source.osm.pbf").write_bytes(b"raw")
    result = subprocess.run(
        [BASH, "-e", "-o", "pipefail", "-c", script], cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": (tmp_path / "runner-temp").as_posix()},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "the raw extract was not deleted" in result.stdout


# ---------------------------------------------------------------------------
# The route corpus gates the publish
# ---------------------------------------------------------------------------

CORPUS_STEP = "Check the route corpus"


def test_the_corpus_gates_every_upload(jobs):
    """Every step that writes to the release runs after the corpus has passed,
    and the corpus runs on the verified manifest — the one being published."""
    steps = jobs["publish"]["steps"]
    names = [step.get("name") for step in steps]
    gate = names.index(CORPUS_STEP)
    assert names.index("Build and verify the manifest") < gate
    writers = [i for i, step in enumerate(steps)
               if "gh release upload" in step.get("run", "")
               or "gh release create" in step.get("run", "")]
    assert writers, "the upload step is gone, not the gate"
    assert all(gate < i for i in writers)


def test_the_corpus_gate_is_skipped_only_under_force_publish(jobs):
    """No `if:` can skip the step; the shell's own check of `force_publish` is
    the one way past it, and is exercised for real below."""
    step = _step(jobs["publish"], CORPUS_STEP)
    assert "if" not in step
    assert step["env"]["FORCE"] == "${{ inputs.force_publish }}"
    # Inputs reach the shell through env only: `${{ }}` inside `run:` is
    # script injection from a dispatch form.
    assert "${{" not in step["run"]


def test_the_corpus_gate_adds_no_job(jobs):
    """It is a step of `publish`, so nothing new holds `contents: write`
    (test_only_the_publish_job_can_write checks who does)."""
    assert set(jobs) == {"plan", "build", "publish", "notify"}
    assert "scripts/route_corpus.py" in _step(jobs["publish"], CORPUS_STEP)["run"]


def test_the_publish_job_installs_what_the_corpus_imports(jobs):
    """The runner resolves legs with the server's resolver, which imports
    metrics, redis and SQLAlchemy as well as pyosmium for the builder."""
    assert "pip install -r requirements.txt" in _steps_text(jobs["publish"])


CARRIED_BYTES = b"carried sweden\n"


def _run_corpus_step(tmp_path, jobs, *, requested="", force="",
                     corpus_exit=0, carried_sha=None, layered=False):
    """Run the corpus step for real against a fake `gh` and `python`.

    The manifest holds Denmark (built by this run, in dist/rail), Sweden (ok
    but carried: only the release has it), France (ok, not in the corpus) and
    Andorra (in the corpus, but `empty`). The real python lists what to build,
    so reading the manifest and the corpus is the step's own code; the builder
    and the runner are faked and log their arguments.

    *layered* makes it a schema 3 manifest: every entry gets `layer: rail`,
    and Denmark and Sweden gain `ok` ferry and bus entries of their own.
    """
    script = _step(jobs["publish"], CORPUS_STEP)["run"]
    work = tmp_path / "work"
    (work / "dist" / "rail").mkdir(parents=True)
    (work / "config").mkdir()
    (work / "config" / "route_corpus.yml").write_text(
        "legs:\n"
        "  - {name: a, regions: [europe/denmark, europe/sweden]}\n"
        "  - {name: b, regions: [europe/andorra]}\n",
        encoding="utf-8",
    )
    (work / "dist" / "rail" / "denmark-rail.osm.pbf").write_bytes(b"built")
    sha = carried_sha or hashlib.sha256(CARRIED_BYTES).hexdigest()

    def ok(region, file, digest="0" * 64):
        return {"region": region, "status": "ok", "file": file,
                "sha256": digest, "bbox": [0, 0, 1, 1]}

    regions = [ok("europe/denmark", "denmark-rail.osm.pbf"),
               ok("europe/sweden", "sweden-rail.osm.pbf", sha),
               ok("europe/france", "france-rail.osm.pbf"),
               {"region": "europe/andorra", "status": "empty"}]
    if layered:
        regions = [{**e, "layer": "rail"} for e in regions]
        for slug in ("denmark", "sweden"):
            for layer in ("ferry", "bus"):
                (work / "dist" / "rail" / f"{slug}-{layer}.osm.pbf").write_bytes(b"x")
                regions.append({**ok(f"europe/{slug}", f"{slug}-{layer}.osm.pbf"),
                                "layer": layer})
    (work / "dist" / "rail" / "manifest.json").write_text(json.dumps({
        "schema": 3 if layered else 2, "regions": regions,
    }), encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    (fake_bin / "gh").write_text(
        chr(10).join([
            "#!/bin/sh",
            f'printf "gh %s\\n" "$*" >> "{log.as_posix()}"',
            # release download TAG --pattern FILE --dir DIR --clobber
            'if [ "$1 $2" = "release download" ]; then',
            f'  printf "%s\\n" "{CARRIED_BYTES.decode().strip()}" > "$7/$5"',
            "fi",
            "exit 0",
            "",
        ]),
        encoding="utf-8", newline="\n",
    )
    (fake_bin / "python").write_text(
        chr(10).join([
            "#!/bin/bash",
            "set -o pipefail",
            f'printf "python %s\\n" "$*" >> "{log.as_posix()}"',
            'case "$1" in',
            # Windows' python ends its lines with CRLF; Linux's never does.
            f'  -c) "{Path(sys.executable).as_posix()}" "$@" | tr -d "\\r" ;;',
            '  -m) [ -f "$3" ] || exit 3; : > "$4" ;;',
            '  scripts/route_corpus.py) [ -f "$2/manifest.json" ] || exit 4;'
            f" exit {corpus_exit} ;;",
            "  *) exit 5 ;;",
            "esac",
            "",
        ]),
        encoding="utf-8", newline="\n",
    )
    for tool in ("gh", "python"):
        (fake_bin / tool).chmod(0o755)

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = subprocess.run(
        # What Actions runs a `run:` with: bash -e -o pipefail.
        [BASH, "-e", "-o", "pipefail", "-c", script],
        cwd=work,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
             "PYTHONPATH": str(ROOT), "RUNNER_TEMP": runner_temp.as_posix(),
             "TAG": "rail-data-2026-10-05", "REQUESTED": requested,
             "FORCE": force},
        capture_output=True, text=True,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result.returncode, result.stdout + result.stderr, calls, runner_temp


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize("requested, require_all",
                         [("", True), ("europe/denmark", False)],
                         ids=["full run requires every region",
                              "subset run lets an uncovered region skip"])
def test_the_corpus_runs_on_built_and_carried_stores(jobs, tmp_path,
                                                     requested, require_all):
    """Stores come from this run's extract where it built one and from the
    release being patched where it carries one; a region outside the corpus,
    or not `ok`, is never built."""
    code, output, calls, temp = _run_corpus_step(tmp_path, jobs,
                                                 requested=requested)
    assert code == 0, output
    stores = f"{temp.as_posix()}/rail-stores"
    carried = f"{temp.as_posix()}/rail-carried"
    assert (f"python -m src.rail.builder dist/rail/denmark-rail.osm.pbf "
            f"{stores}/europe-denmark.rail.sqlite --region europe/denmark "
            f"--layer rail") in calls
    assert ("gh release download rail-data-2026-10-05 --pattern "
            f"sweden-rail.osm.pbf --dir {carried}") in calls
    assert (f"python -m src.rail.builder {carried}/sweden-rail.osm.pbf "
            f"{stores}/europe-sweden.rail.sqlite --region europe/sweden "
            f"--layer rail") in calls
    assert calls.count("src.rail.builder") == 2
    assert "denmark-rail.osm.pbf --dir" not in calls
    corpus = [line for line in calls.splitlines() if "route_corpus.py" in line]
    assert corpus == [f"python scripts/route_corpus.py {stores}"
                      + (" --require-all" if require_all else "")]
    assert calls.rindex("src.rail.builder") < calls.index("route_corpus.py")


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize("requested", ["", "europe/denmark"],
                         ids=["full run", "subset run"])
def test_the_corpus_builds_only_rail_from_a_layered_manifest(jobs, tmp_path, requested):
    """The corpus is rail legs. A ferry or bus entry names another file for
    the same region, and building it as that region's rail store would gate
    the release on the wrong data — or overwrite the rail store with it."""
    code, output, calls, temp = _run_corpus_step(tmp_path, jobs, layered=True,
                                                 requested=requested)
    assert code == 0, output
    builds = [line for line in calls.splitlines() if "src.rail.builder" in line]
    assert len(builds) == 2, builds
    assert all(line.endswith("--layer rail") for line in builds), builds
    assert all("-rail.osm.pbf " in line and ".rail.sqlite " in line for line in builds)
    assert "-ferry.osm.pbf" not in calls and "-bus.osm.pbf" not in calls


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
def test_a_failing_corpus_fails_the_step(jobs, tmp_path):
    code, output, calls, _ = _run_corpus_step(tmp_path, jobs, corpus_exit=1)
    assert code != 0, output
    assert "route_corpus.py" in calls


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
def test_a_carried_extract_that_does_not_match_the_manifest_fails(jobs, tmp_path):
    """The manifest about to be published vouches for the carried asset's
    checksum; a mismatch is a release the box would refuse at install."""
    code, output, calls, _ = _run_corpus_step(tmp_path, jobs,
                                              requested="europe/denmark",
                                              carried_sha="f" * 64)
    assert code != 0, output
    assert "sweden-rail.osm.pbf --region" not in calls
    assert "route_corpus.py" not in calls


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
def test_force_publish_skips_the_corpus_and_says_so(jobs, tmp_path):
    code, output, calls, _ = _run_corpus_step(tmp_path, jobs, force="true",
                                              corpus_exit=1)
    assert code == 0, output
    assert "::warning::force_publish: the route corpus gate was skipped" in output
    assert calls == ""
