"""The project is licensed under AGPL-3.0 (issue #421).

Before this there was no LICENSE file at all, while ``.env.example``, the
README, the billing docs and the landing page all told readers the code was
MIT-licensed. Without a licence file that claim granted nothing. These tests
pin the licence text, keep the old claim from coming back, and check that the
places offering users the source (AGPL section 13) name the same repository.
"""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from src.brand import REPO_URL

ROOT = Path(__file__).resolve().parent.parent

# sha256 of https://www.gnu.org/licenses/agpl-3.0.txt with LF line endings.
_AGPL_3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_license_is_the_canonical_agpl_3_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8").replace("\r\n", "\n")

    assert text.lstrip().startswith(
        "GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3, 19 November 2007"
    )
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _AGPL_3_SHA256


def test_env_example_no_longer_claims_mit():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert not re.search(r"\bMIT\b", text)
    assert "AGPL-3.0" in text


def test_no_tracked_file_claims_the_mit_licence():
    """The repo said MIT in half a dozen places before it had a licence at all."""
    found = []
    for rel in _tracked_files():
        path = ROOT / rel
        # The vendored fonts and their licences are theirs, not ours.
        if not path.is_file() or rel == "tests/test_license.py" or rel.startswith("assets/fonts/"):
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bMIT\b", line):
                found.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not found, "MIT licence claims remain:\n" + "\n".join(found)


def test_the_image_declares_its_licence():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.findall(r'org\.opencontainers\.image\.licenses="([^"]*)"', text) == ["AGPL-3.0-only"]


def test_the_app_links_the_same_repository_as_the_server():
    """The Settings "Source code" link and the pages must point at one repository."""
    dart = (ROOT / "flutter_client" / "lib" / "src" / "core" / "brand.dart").read_text(encoding="utf-8")

    assert re.findall(r"const kRepoUrl = '([^']*)';", dart) == [REPO_URL]
