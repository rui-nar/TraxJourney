"""The image installs the locked runtime set with uv sync (#603)."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
LINES = [line.strip() for line in DOCKERFILE.splitlines()]
DOCKERIGNORE = [
    line.strip()
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

LOCK_FILES = ("pyproject.toml", "uv.lock", ".python-version")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _uv_tag() -> str:
    match = re.search(r"^FROM ghcr\.io/astral-sh/uv:(\S+) AS uv$", DOCKERFILE, re.M)
    assert match, "no `FROM ghcr.io/astral-sh/uv:<tag> AS uv` stage"
    return match.group(1)


def _index(predicate) -> int:
    return next(i for i, line in enumerate(LINES) if predicate(line))


def test_no_pip_install():
    assert "pip install" not in DOCKERFILE


def test_uv_sync_is_locked_and_runtime_only():
    syncs = [line for line in LINES if line.startswith("RUN uv sync")]
    assert len(syncs) == 1, syncs
    flags = syncs[0].split()
    assert "--locked" in flags
    assert "--no-default-groups" in flags
    assert "--frozen" not in flags


def test_uv_binary_comes_from_the_pinned_stage():
    assert "COPY --from=uv /uv /bin/uv" in LINES


def test_uv_stage_tag_is_an_exact_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _uv_tag()), _uv_tag()


def test_required_version_floor_is_no_newer_than_the_uv_stage():
    # The floor does not have to follow the tag (R3-1), only never pass it.
    floor = re.fullmatch(r">=(\d+\.\d+\.\d+)", PYPROJECT["tool"]["uv"]["required-version"])
    assert floor, PYPROJECT["tool"]["uv"]["required-version"]
    assert _version(floor.group(1)) <= _version(_uv_tag())


def test_venv_bin_comes_first_on_path():
    assert re.search(r'PATH="/app/\.venv/bin:\$PATH"', DOCKERFILE)


def test_lock_files_are_copied_before_the_code():
    lock_copy = _index(lambda l: l.startswith("COPY ") and all(f in l.split() for f in LOCK_FILES))
    sync = _index(lambda l: l.startswith("RUN uv sync"))
    code_copy = _index(lambda l: l == "COPY . .")
    assert lock_copy < sync < code_copy


def test_dockerignore_keeps_the_venv_out_and_the_lock_files_in():
    assert ".venv/" in DOCKERIGNORE
    for name in LOCK_FILES:
        assert name not in DOCKERIGNORE, name
