"""The dependency set lives in pyproject.toml and is locked in uv.lock (#603)."""
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0].lower().replace("_", "-")


def _specs(entries) -> dict[str, str]:
    return {_name(e): e for e in entries if isinstance(e, str)}


RUNTIME = _specs(PYPROJECT["project"]["dependencies"])
GROUPS = PYPROJECT["dependency-groups"]
UV = PYPROJECT["tool"]["uv"]


def test_runtime_dependencies_exclude_test_and_release_tooling():
    for name in ("pytest", "pytest-cov", "fakeredis", "pyyaml", "anthropic"):
        assert name not in RUNTIME, name


def test_the_server_and_in_image_scripts_dependencies_are_runtime():
    for name in ("httpx", "osmium", "requests"):
        assert name in RUNTIME, name


def test_rail_group_is_exactly_osmium_requests_and_pyyaml():
    rail = _specs(GROUPS["rail"])
    assert set(rail) == {"osmium", "requests", "pyyaml"}
    assert rail["osmium"] == RUNTIME["osmium"]
    assert rail["requests"] == RUNTIME["requests"]


def test_dev_group_includes_rail():
    assert {"include-group": "rail"} in GROUPS["dev"]
    assert {"pytest", "pytest-cov", "fakeredis"} <= set(_specs(GROUPS["dev"]))


def test_anthropic_is_only_in_the_release_group():
    holders = [g for g, entries in GROUPS.items() if "anthropic" in _specs(entries)]
    assert holders == ["release"]


def test_a_plain_sync_gives_a_developer_everything():
    assert UV["default-groups"] == ["dev", "release"]
    assert UV["package"] is False


def test_required_uv_is_a_floor_not_a_pin():
    assert re.fullmatch(r">=\d+\.\d+\.\d+", UV["required-version"]), UV["required-version"]


def test_sqlmodel_and_sqlalchemy_stay_exact_pins():
    for name in ("sqlmodel", "sqlalchemy"):
        assert "==" in RUNTIME[name], RUNTIME[name]


def test_the_lock_exists_and_targets_the_same_python():
    assert LOCK["requires-python"] == PYPROJECT["project"]["requires-python"]


# Historical plans, reviews and repro notes legitimately name the old pip path.
_PIP_PATH_EXEMPT = ("docs/", "graphify-out/", "tools/", "flutter_client/", "uv.lock",
                    "tests/test_python_packaging.py")


def test_requirements_txt_is_gone():
    assert not (ROOT / "requirements.txt").exists()


def test_nothing_outside_the_historical_documents_reverts_to_pip():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    offenders = []
    for rel in filter(None, tracked):
        if rel.startswith(_PIP_PATH_EXEMPT):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="ignore")
        if "requirements.txt" in text or re.search(r"pip\s+install\s+(\S+\s+)*-r", text):
            offenders.append(rel)
    assert not offenders, offenders
