"""The Python version must be the same in production, CI and local dev.

The image the API actually runs on is pinned in the Dockerfile; CI installs
whatever `.python-version` names, through uv. Nothing tied the two together, so
they could drift apart silently — and a third copy, whatever interpreter the
developer's venv happens to hold, drifts on its own the moment a new Python is
installed on the machine.

A mismatch is invisible until it isn't: a 3.12+-only stdlib call passes locally
and on CI and then fails in the container, or the reverse. These assertions are
on the pin sites themselves, because that is where the mistakes live.
"""
import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
PYTHON_VERSION_FILE = ROOT / ".python-version"
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _dockerfile_python() -> str:
    """The X.Y tag off the base image the API is built from."""
    match = re.search(r"^FROM python:(\d+\.\d+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert match, "Dockerfile has no `FROM python:X.Y` line to read a version from"
    return match.group(1)


def test_ci_pins_the_same_python_as_the_production_image():
    """`.python-version` is what uv installs in CI and locally."""
    pinned = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    expected = _dockerfile_python()
    assert pinned == expected, (
        f"CI runs Python {pinned} (.python-version) but the Dockerfile ships "
        f"{expected}; tests would be proving something about an interpreter "
        "production never runs"
    )


def test_requires_python_names_the_production_python():
    requires = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"]
    match = re.fullmatch(r">=(\d+\.\d+)", requires.strip())
    assert match, f"requires-python is {requires!r}, expected a `>=X.Y` floor"
    assert match.group(1) == _dockerfile_python()


def test_no_workflow_pins_its_own_python():
    """A `python-version` in a workflow is a second CI pin that nothing ties
    to the image's; `.python-version` must stay the only one."""
    assert WORKFLOWS, "no workflows found"
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                assert "python-version" not in step.get("with", {}), (path.name, name)


def test_the_local_interpreter_matches_the_production_image():
    """Not a hard failure — a warning, so a stale venv is visible, not blocking."""
    expected = _dockerfile_python()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running != expected:
        pytest.skip(
            f"this venv runs Python {running} but production ships {expected} — "
            "recreate the venv on the pinned version before trusting a local green run"
        )
