"""CI installs Python dependencies with uv, from the lock, and checks the image (#603).

Every build pins uv to one exact version: the Dockerfile's uv stage and every
setup-uv step. Dependabot moves the Dockerfile's tag; the equality below is
what makes that PR fail until the workflows move with it, which is how a uv
update reaches the owner (docs/UV_MIGRATION_PLAN.md, Decision 4).

The suite itself runs with every dependency group installed, so it cannot see
server code that needs a dev-only package. Two checks in test.yml can: an
import from the runtime set alone on every PR, and a build of the image itself
when a change touches what it is built from (Decision 6).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_rail_extract_workflow import BASH

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
UV_WORKFLOWS = ["test.yml", "rail-extract.yml", "android-release.yml"]
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
DOCKERFILE = ROOT / "Dockerfile"

IMAGE_INPUTS = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock",
                ".python-version", "entrypoint.sh", "worker-entrypoint.sh"]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _steps(workflow: dict):
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


def _step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _dockerfile_uv() -> str:
    """The tag of the Dockerfile's `FROM ghcr.io/astral-sh/uv:X.Y.Z AS uv`."""
    match = re.search(r"^FROM ghcr\.io/astral-sh/uv:(\S+) AS uv\s*$",
                      DOCKERFILE.read_text(encoding="utf-8"), re.M | re.I)
    assert match, "Dockerfile has no `FROM ghcr.io/astral-sh/uv:X.Y.Z AS uv` stage"
    return match.group(1)


# ---------------------------------------------------------------------------
# uv, from the lock, everywhere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_uses_pip_or_setup_python(path):
    text = path.read_text(encoding="utf-8")
    assert "pip install" not in text
    assert "setup-python" not in text


def test_every_setup_uv_pins_the_dockerfiles_uv():
    """Exactly, not a range: a uv bump in the Dockerfile fails here until CI
    runs the same uv."""
    expected = _dockerfile_uv()
    assert re.fullmatch(r"\d+\.\d+\.\d+", expected), \
        f"the Dockerfile's uv stage is {expected!r}, not an exact X.Y.Z"
    found = []
    for path in WORKFLOWS:
        for job, step in _steps(yaml.safe_load(path.read_text(encoding="utf-8"))):
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                found.append((path.name, job))
                version = step.get("with", {}).get("version")
                assert version == expected, (path.name, job, version, expected)
    assert {name for name, _ in found} == set(UV_WORKFLOWS)


def test_every_setup_uv_ref_is_an_exact_tag():
    """Upstream publishes no major or minor tags after v7 (v8.0.0 release
    notes), so `@v10` does not resolve and `@v7` is frozen. An exact,
    immutable `vX.Y.Z` tag is what Dependabot's github-actions updates move."""
    refs = [step["uses"].split("@", 1)[1]
            for path in WORKFLOWS
            for _, step in _steps(yaml.safe_load(path.read_text(encoding="utf-8")))
            if step.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert refs, "no setup-uv step found"
    for ref in refs:
        assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), ref


@pytest.mark.parametrize("name", UV_WORKFLOWS)
def test_every_uv_sync_is_locked(name):
    """`--locked` fails on a lock that does not match pyproject.toml rather
    than resolving afresh; `--frozen` would skip that check."""
    syncs = [line for _, step in _steps(_load(name))
             for line in re.findall(r"uv sync[^\n]*", step.get("run", ""))]
    assert syncs, f"{name} syncs nothing"
    for line in syncs:
        assert "--locked" in line.split(), line
        assert "--frozen" not in line, line


@pytest.mark.parametrize("name", UV_WORKFLOWS)
def test_nothing_resyncs_after_the_explicit_sync(name):
    """Without UV_NO_SYNC every `uv run` re-syncs to the default groups first,
    undoing the rail jobs' narrower sync on every matrix leg."""
    assert _load(name)["env"]["UV_NO_SYNC"] == "1"


@pytest.mark.parametrize("name", UV_WORKFLOWS)
def test_every_job_that_runs_uv_syncs_once(name):
    for job_name, job in _load(name)["jobs"].items():
        text = "\n".join(step.get("run", "") for step in job.get("steps", []))
        if "uv run" not in text:
            continue
        uses = [step.get("uses", "") for step in job["steps"]]
        assert any(u.startswith("astral-sh/setup-uv@") for u in uses), job_name
        assert len(re.findall(r"^\s*uv sync\b", text, re.M)) == 1, job_name


def test_the_test_job_syncs_every_group_and_runs_through_uv():
    steps = _load("test.yml")["jobs"]["test"]["steps"]
    assert _step({"steps": steps}, "Install dependencies")["run"] == "uv sync --locked"
    migrations = _step({"steps": steps}, "Check migrations are in sync with models")["run"]
    assert "uv run alembic upgrade head" in migrations
    assert "uv run alembic check" in migrations
    assert _step({"steps": steps}, "Run tests")["run"].startswith("uv run pytest tests/ ")


# ---------------------------------------------------------------------------
# The runtime set alone imports the server, on every PR
# ---------------------------------------------------------------------------

RUNTIME_STEP = "Import the server from the runtime dependencies alone"


def test_the_runtime_only_import_runs_in_the_test_job():
    job = _load("test.yml")["jobs"]["test"]
    names = [step.get("name") for step in job["steps"]]
    assert names.index(RUNTIME_STEP) > names.index("Run tests")
    step = _step(job, RUNTIME_STEP)
    assert "if" not in step
    run = step["run"]
    sync = re.search(r"^UV_PROJECT_ENVIRONMENT=(\S+) uv sync (.*)$", run, re.M)
    assert sync, run
    env_dir, flags = sync.groups()
    assert env_dir.startswith("$RUNNER_TEMP/"), "never the job's own .venv"
    assert flags.split() == ["--locked", "--no-default-groups"]
    assert f'"{env_dir}/bin/python" -c "import api.router, src.jobs.worker"' in run
    assert f'"{env_dir}/bin/python" scripts/fetch_rail_data.py --help' in run


def test_both_import_checks_import_with_the_same_environment():
    jobs = _load("test.yml")["jobs"]
    runtime = _step(jobs["test"], RUNTIME_STEP)["env"]
    image = _step(jobs["image"], "Smoke-test the image")["env"]
    assert runtime == image
    assert runtime["DATABASE_URL"].startswith("sqlite:")


# ---------------------------------------------------------------------------
# The image is built and smoke-tested when a change touches it
# ---------------------------------------------------------------------------

def test_the_image_job_builds_and_checks_the_image():
    job = _load("test.yml")["jobs"]["image"]
    build = _step(job, "Build the image")
    smoke = _step(job, "Smoke-test the image")
    for step in (build, smoke):
        assert step["if"] == "steps.filter.outputs.build == 'true'"
    assert build["run"].startswith("docker build ")
    run = smoke["run"]
    assert '-c "import api.router, src.jobs.worker"' in run
    assert '-c "import osmium"' in run
    assert 'if docker run --rm --entrypoint python "$image" -c "import pytest"; then' in run
    assert "--entrypoint alembic" in run


def test_the_image_job_needs_no_secret_and_pushes_nothing():
    text = yaml.safe_dump(_load("test.yml")["jobs"]["image"])
    assert "secrets." not in text
    assert "docker push" not in text
    assert "docker/login-action" not in text


def _run_filter(tmp_path, event: str, changed: list[str]) -> tuple[int, str, str | None]:
    """Run the filter step for real against a fake `git` that reports *changed*."""
    job = _load("test.yml")["jobs"]["image"]
    script = _step(job, "Does this change touch the image?")["run"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    listing = tmp_path / "changed.txt"
    listing.write_text("".join(f"{path}\n" for path in changed), encoding="utf-8")
    log = tmp_path / "git.log"
    (fake_bin / "git").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log.as_posix()}"\n'
        f'cat "{listing.as_posix()}"\n',
        encoding="utf-8", newline="\n",
    )
    (fake_bin / "git").chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
           "GITHUB_OUTPUT": output.as_posix(), "EVENT": event}
    result = subprocess.run([BASH, "-e", "-o", "pipefail", "-c", script],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in
                   output.read_text(encoding="utf-8").splitlines() if line)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result.returncode, calls, outputs.get("build")


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize("path", IMAGE_INPUTS)
def test_a_pr_touching_an_image_input_builds(tmp_path, path):
    code, calls, build = _run_filter(tmp_path, "pull_request",
                                     ["api/router.py", path, "README.md"])
    assert code == 0
    assert calls.strip() == "diff --name-only HEAD^1 HEAD"
    assert build == "true"


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
@pytest.mark.parametrize("changed", [
    ["api/router.py", "README.md"],
    ["scripts/Dockerfile.notes", "docs/uv.lock.md", "tests/entrypoint.sh"],
    [],
], ids=["code only", "names that only contain an input", "nothing"])
def test_a_pr_not_touching_the_image_skips_the_build(tmp_path, changed):
    code, _, build = _run_filter(tmp_path, "pull_request", changed)
    assert code == 0
    assert build == "false"


@pytest.mark.skipif(BASH is None, reason="needs a working POSIX shell")
def test_a_push_to_main_always_builds(tmp_path):
    code, calls, build = _run_filter(tmp_path, "push", [])
    assert code == 0
    assert calls == ""
    assert build == "true"


def test_the_filter_can_see_the_base():
    """A shallow clone of depth 1 has no HEAD^1, and the diff would fail."""
    job = _load("test.yml")["jobs"]["image"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 2


# ---------------------------------------------------------------------------
# Dependabot
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dependabot() -> dict:
    return yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))


def _ecosystem(dependabot: dict, name: str) -> dict:
    matches = [u for u in dependabot["updates"] if u["package-ecosystem"] == name]
    assert len(matches) == 1, name
    return matches[0]


def test_dependabot_covers_the_lock_uv_and_the_actions(dependabot):
    assert dependabot["version"] == 2
    assert {u["package-ecosystem"] for u in dependabot["updates"]} == \
        {"uv", "docker", "github-actions"}
    for update in dependabot["updates"]:
        assert update["schedule"]["interval"] == "weekly"
    uv = _ecosystem(dependabot, "uv")
    assert uv["directory"] == "/"
    assert [g["update-types"] for g in uv["groups"].values()] == [["minor", "patch"]]
    assert _ecosystem(dependabot, "github-actions")["groups"]


def test_dependabot_never_proposes_a_new_cpython_minor(dependabot):
    """A CPython minor moves .python-version, the Dockerfile and
    requires-python together; the alignment test would reject a PR that
    moves the Dockerfile alone."""
    docker = _ecosystem(dependabot, "docker")
    assert docker["directory"] == "/"
    ignored = [i for i in docker.get("ignore", []) if i["dependency-name"] == "python"]
    assert len(ignored) == 1
    assert set(ignored[0]["update-types"]) == {
        "version-update:semver-minor", "version-update:semver-major"}
