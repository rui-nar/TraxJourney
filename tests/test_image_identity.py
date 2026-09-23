"""The published image, its repository link and the deploy files agree (#151).

The image path is written out in the build workflow and again in the compose
example a deployment is copied from, and the repository it belongs to is
written in the Dockerfile, the workflow and ``src/brand.py``. Nothing at build
or run time compares them: a workflow pushing one name while the compose file
pulls another deploys a stale image with no error, and a package whose source
label names the wrong repository is simply not linked to it on GHCR. The
validation webhook, in turn, matches the workflow by its display name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.brand import REPO_URL

ROOT = Path(__file__).resolve().parent.parent
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "docker-build.yml"
COMPOSE = ROOT / "docker-compose.yml.example"
DOCKERFILE = ROOT / "Dockerfile"
HOOKS = ROOT / "vps" / "webhook" / "hooks.yaml.example"

_GHCR_IMAGE = re.compile(r"ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+")
_SOURCE_LABEL = "org.opencontainers.image.source"


def _repo_image() -> str:
    """The GHCR path GitHub derives for this repository: owner/name, lower-cased."""
    owner, name = REPO_URL.removeprefix("https://github.com/").split("/")
    return f"ghcr.io/{owner}/{name}".lower()


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def build_step(workflow) -> dict:
    steps = workflow["jobs"]["build-and-push"]["steps"]
    (step,) = [s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action")]
    return step


def test_the_workflow_pushes_only_the_repository_image():
    pushed = set(_GHCR_IMAGE.findall(BUILD_WORKFLOW.read_text(encoding="utf-8")))
    assert pushed == {_repo_image()}


def test_the_compose_example_pulls_the_image_the_workflow_pushes():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    pulled = {
        s["image"].split(":")[0]
        for s in services.values()
        if s.get("image", "").startswith("ghcr.io/")
    }
    assert pulled == {_repo_image()}


def test_the_build_labels_the_package_with_the_repository(build_step):
    """A package pushed with a PAT is linked to its repository only by this label."""
    labels = str(build_step["with"].get("labels", "")).split()
    assert f"{_SOURCE_LABEL}={REPO_URL}" in labels


def test_the_dockerfile_labels_the_image_with_the_repository():
    text = DOCKERFILE.read_text(encoding="utf-8")
    found = re.findall(rf'{re.escape(_SOURCE_LABEL)}="([^"]*)"', text)
    assert found == [REPO_URL]


def test_the_validation_webhook_matches_the_build_workflow_name(workflow):
    hooks = yaml.safe_load(HOOKS.read_text(encoding="utf-8"))
    (hook,) = [h for h in hooks if h["id"] == "deploy-validation"]
    names = [
        rule["match"]["value"]
        for rule in hook["trigger-rule"]["and"]
        if rule["match"].get("parameter", {}).get("name") == "workflow_run.name"
    ]
    assert names == [workflow["name"]]
