"""The post-deploy audit scripts ship in the API image.

The PRs that added them (#468, #471) say to run them against each instance's
database after deploying, and the database lives with the container. But
``.dockerignore`` drops ``scripts/`` from the build context, so without an
explicit exception the file is not in the image and
``docker compose run --rm --entrypoint python traxjourney scripts/<audit>.py``
dies with ``can't open file``. Their own tests import them from the checkout,
so nothing else notices.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_rail_data_fetch import _dockerignore_excludes

ROOT = Path(__file__).resolve().parent.parent

AUDITS = ["scripts/audit_activity_ownership.py", "scripts/audit_photo_names.py"]


def _patterns() -> list[str]:
    return (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("script", AUDITS)
def test_audit_script_is_in_the_image(script):
    assert (ROOT / script).is_file()
    assert not _dockerignore_excludes(_patterns(), script)


@pytest.mark.parametrize("package", ["models/project_db.py", "src/utils/photo_paths.py"])
def test_what_the_audits_import_is_in_the_image(package):
    assert not _dockerignore_excludes(_patterns(), package)
