"""The validation auto-deploy only fires for a real build, and proves it landed (#422).

`vps/webhook/` is installed by hand on the VPS and nothing exercises it until
a `validation` build finishes, so a wrong rule shows up as either silence (the
deploy never runs, which is how #205's version went unnoticed) or a deploy
anyone can trigger. The hook rules are checked here field by field against
what GitHub sends, and the deploy script is run against a fake `docker` and
`curl` so its lock and its version check are exercised, not just read.

The workflow-name rule is pinned in ``tests/test_image_identity.py``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from src.brand import REPO_URL

ROOT = Path(__file__).resolve().parent.parent
WEBHOOK_DIR = ROOT / "vps" / "webhook"
HOOKS = WEBHOOK_DIR / "hooks.yaml.example"
SCRIPT = WEBHOOK_DIR / "deploy-validation.sh"
UNIT = WEBHOOK_DIR / "webhook.service"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "docker-build.yml"
DEPLOY_DOC = ROOT / "docs" / "DEPLOYMENT_VPS.md"
DEPLOY_PS1 = ROOT / "deploy.ps1"

SHA = "84a156a5f39ca5ac3110d49b284dfd51e0bde3eb"
OTHER_SHA = "6e8eac6dbddb05e19d7e71fcd66f186ceacde5cb"


# ---------------------------------------------------------------------------
# hooks.yaml.example
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hook() -> dict:
    hooks = yaml.safe_load(HOOKS.read_text(encoding="utf-8"))
    (found,) = [h for h in hooks if h["id"] == "deploy-validation"]
    return found


@pytest.fixture(scope="module")
def matches(hook) -> dict:
    """(source, name) -> the match rule reading that request value."""
    rules = hook["trigger-rule"]["and"]
    out = {}
    for rule in rules:
        match = rule["match"]
        key = (match["parameter"]["source"], match["parameter"]["name"])
        assert key not in out, f"two rules read {key}"
        out[key] = match
    return out


def test_every_rule_must_hold(hook):
    """One flat `and` of matches: an `or`/`not` anywhere could bypass the signature."""
    assert set(hook["trigger-rule"]) == {"and"}
    assert all(set(rule) == {"match"} for rule in hook["trigger-rule"]["and"])
    assert not hook.get("trigger-signature-soft-failures", False)


def test_the_payload_signature_is_checked_with_hmac_sha256(matches):
    rule = matches[("header", "X-Hub-Signature-256")]
    assert rule["type"] == "payload-hmac-sha256"
    assert "secret" in rule


def test_the_example_secret_is_empty_so_an_unfilled_copy_rejects_everything(matches):
    """webhook errors on an empty secret; a placeholder would be a public, working key."""
    assert matches[("header", "X-Hub-Signature-256")]["secret"] == ""


@pytest.mark.parametrize(
    "source, name, expected",
    [
        ("header", "X-GitHub-Event", "workflow_run"),
        ("payload", "action", "completed"),
        ("payload", "workflow_run.conclusion", "success"),
        ("payload", "workflow_run.head_branch", "validation"),
    ],
)
def test_only_a_successful_completed_validation_run_triggers(matches, source, name, expected):
    rule = matches[(source, name)]
    assert (rule["type"], rule["value"]) == ("value", expected)


def test_only_this_repository_triggers(matches):
    """GitHub sends the current name, so this follows the rename (#151)."""
    rule = matches[("payload", "repository.full_name")]
    full_name = REPO_URL.removeprefix("https://github.com/")
    assert (rule["type"], rule["value"]) == ("value", full_name)
    assert full_name == "rui-nar/TraxJourney"


def test_the_validation_tag_is_what_builds_the_image(matches):
    """For a tag push, workflow_run.head_branch is the tag name."""
    workflow = yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as boolean True.
    tags = (workflow.get("on") or workflow[True])["push"]["tags"]
    assert matches[("payload", "workflow_run.head_branch")]["value"] in tags


def test_the_built_sha_is_passed_to_the_script(hook):
    assert hook["pass-arguments-to-command"] == [
        {"source": "payload", "name": "workflow_run.head_sha"}
    ]
    assert Path(hook["execute-command"]).name == SCRIPT.name


def test_the_build_names_validation_versions_the_way_the_script_expects():
    """The script accepts validation-<7+ hex digits of the built sha>."""
    text = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert 'version="validation-$(git rev-parse --short "${{ github.sha }}")"' in text


# ---------------------------------------------------------------------------
# webhook.service and the documented Caddy route
# ---------------------------------------------------------------------------

def _exec_start() -> list[str]:
    (line,) = [l for l in UNIT.read_text(encoding="utf-8").splitlines() if l.startswith("ExecStart=")]
    return line.removeprefix("ExecStart=").split()


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_the_listener_binds_loopback_only():
    assert _flag(_exec_start(), "-ip") == "127.0.0.1"


def test_the_unit_loads_the_hooks_file_next_to_the_script(hook):
    hooks_path = Path(_flag(_exec_start(), "-hooks"))
    assert hooks_path.name == "hooks.yaml"
    assert hooks_path.parent == Path(hook["execute-command"]).parent


def test_the_documented_caddy_route_and_payload_url_reach_the_listener(hook):
    doc = DEPLOY_DOC.read_text(encoding="utf-8")
    port = _flag(_exec_start(), "-port")
    route = re.search(r"handle_path (/[\w-]+)/\* \{\s*reverse_proxy 127\.0\.0\.1:(\d+)", doc)
    assert route, "no handle_path route to the webhook listener in DEPLOYMENT_VPS.md"
    prefix, routed_port = route.groups()
    assert routed_port == port
    # webhook serves each hook at /hooks/<id> (its default -urlprefix).
    assert f"https://val.traxjourney.com{prefix}/hooks/{hook['id']}" in doc


@pytest.mark.skipif(not DEPLOY_PS1.exists(), reason="deploy.ps1 is gitignored, local-only")
def test_the_unit_runs_as_the_user_deploy_ps1_connects_as():
    """The unit said User=debian while every manual deploy runs as another user."""
    deploy_user = re.search(r'^\$VPS_USER\s*=\s*"([^"]+)"', DEPLOY_PS1.read_text(encoding="utf-8-sig"), re.M)
    unit_user = re.search(r"^User=(\S+)$", UNIT.read_text(encoding="utf-8"), re.M)
    assert deploy_user and unit_user
    assert unit_user.group(1) == deploy_user.group(1)


# ---------------------------------------------------------------------------
# deploy-validation.sh, run against a fake docker and curl
# ---------------------------------------------------------------------------

def _shell_with_flock() -> str | None:
    """bash that runs and has flock. A WSL `bash` shim without a distro fails the probe."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        probe = subprocess.run([bash, "-c", "command -v flock >/dev/null && exit 7"], capture_output=True)
    except OSError:
        return None
    return bash if probe.returncode == 7 else None


BASH = _shell_with_flock()
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash with flock")

_FAKE_DOCKER = """#!/bin/sh
if flock -n "$LOCK_FILE" true; then lock=free; else lock=held; fi
echo "$(pwd)|$lock|$*" >> "$DOCKER_CALLS"
"""

# FAKE_VERSIONS: one answer per call, the last one repeating; "down" = no answer.
_FAKE_CURL = """#!/bin/sh
n=$(( $(cat "$CURL_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$CURL_COUNT"
v=$(echo "$FAKE_VERSIONS" | awk -v n="$n" '{ print (n <= NF) ? $n : $NF }')
[ "$v" = "down" ] && exit 7
printf '{"version":"%s"}' "$v"
"""


class Deploy:
    def __init__(self, tmp_path: Path):
        self.val_dir = tmp_path / "val"
        self.hook_dir = tmp_path / "hook"
        bin_dir = tmp_path / "bin"
        for d in (self.val_dir, self.hook_dir, bin_dir):
            d.mkdir()
        for name, body in (("docker", _FAKE_DOCKER), ("curl", _FAKE_CURL)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.log = self.hook_dir / "deploy.log"
        self.lock = self.hook_dir / "deploy.lock"
        self.docker_calls = tmp_path / "docker_calls"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "VAL_DIR": str(self.val_dir),
            "HOOK_DIR": str(self.hook_dir),
            "LOG_FILE": str(self.log),
            "LOCK_FILE": str(self.lock),
            "LOCK_WAIT": "1",
            "VERIFY_ATTEMPTS": "3",
            "VERIFY_INTERVAL": "0",
            "DOCKER_CALLS": str(self.docker_calls),
            "CURL_COUNT": str(tmp_path / "curl_count"),
        }

    def run(self, *args: str, versions: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(SCRIPT), *args],
            env={**self.env, "FAKE_VERSIONS": versions},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[list[str]]:
        if not self.docker_calls.exists():
            return []
        return [line.split("|") for line in self.docker_calls.read_text().splitlines()]


@pytest.fixture
def deploy(tmp_path) -> Deploy:
    return Deploy(tmp_path)


@needs_bash
def test_a_matching_version_succeeds(deploy):
    result = deploy.run(SHA, versions=f"validation-{SHA[:7]}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SUCCESS {SHA[:7]}" in deploy.log.read_text()
    assert [args for _, _, args in deploy.calls()] == ["compose pull", "compose up -d"]
    assert all(Path(cwd) == deploy.val_dir for cwd, _, _ in deploy.calls())


@needs_bash
def test_a_longer_abbreviation_of_the_same_sha_succeeds(deploy):
    assert deploy.run(SHA, versions=f"validation-{SHA[:10]}").returncode == 0


@needs_bash
def test_it_keeps_polling_while_the_new_container_starts(deploy):
    result = deploy.run(SHA, versions=f"down validation-{OTHER_SHA[:7]} validation-{SHA[:7]}")
    assert result.returncode == 0, result.stdout + result.stderr


@needs_bash
def test_a_different_version_fails_loudly(deploy):
    result = deploy.run(SHA, versions=f"validation-{OTHER_SHA[:7]}")
    assert result.returncode != 0
    last = deploy.log.read_text().splitlines()[-1]
    assert f"FAILED {SHA[:7]}" in last and f"validation-{OTHER_SHA[:7]}" in last
    assert "SUCCESS" not in deploy.log.read_text()


@needs_bash
def test_no_answer_at_all_fails(deploy):
    assert deploy.run(SHA, versions="down").returncode != 0


@needs_bash
def test_a_prefix_shorter_than_git_ever_abbreviates_is_not_a_match(deploy):
    assert deploy.run(SHA, versions=f"validation-{SHA[:4]}").returncode != 0


@needs_bash
@pytest.mark.parametrize("arg", [None, "", SHA[:7], SHA.upper(), f"{SHA}; rm -rf /"])
def test_anything_but_a_full_sha_is_refused_before_deploying(deploy, arg):
    result = deploy.run(*([] if arg is None else [arg]), versions=f"validation-{SHA[:7]}")
    assert result.returncode == 2
    assert deploy.calls() == []


@needs_bash
def test_the_deploy_runs_under_the_lock(deploy):
    assert deploy.run(SHA, versions=f"validation-{SHA[:7]}").returncode == 0
    assert [lock for _, lock, _ in deploy.calls()] == ["held", "held"]


@needs_bash
def test_a_deploy_already_holding_the_lock_blocks_a_second_one(deploy):
    holder = subprocess.Popen(
        ["flock", str(deploy.lock), "sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        # Wait until the holder really has it.
        for _ in range(100):
            if subprocess.run(["flock", "-n", str(deploy.lock), "true"]).returncode != 0:
                break
            subprocess.run(["sleep", "0.05"])
        else:
            pytest.fail("the lock holder never took the lock")
        result = deploy.run(SHA, versions=f"validation-{SHA[:7]}")
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert deploy.calls() == []
    assert "held" in deploy.log.read_text().splitlines()[-1]
