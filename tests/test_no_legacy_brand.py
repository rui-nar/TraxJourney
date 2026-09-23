"""No tracked file may still carry the product's old name (issue #151).

The product was renamed ViewTrip -> TraxJourney. A handful of old-name strings
must survive, each for a reason recorded below; anything else is a missed
rename. Scans every git-tracked path and file, so it runs where git and a
checkout exist (CI, a dev checkout) and skips elsewhere.

``flutter_client/test/brand/no_legacy_brand_test.dart`` guards the Flutter
sources with the same allowlist on the Dart side; this test covers the repo.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OLD_NAME = re.compile(r"view[ _-]?trip", re.IGNORECASE)

# path -> (why it may keep the old name, regex every matching line must match)
ALLOWED: dict[str, tuple[str, str]] = {
    "flutter_client/lib/src/crypto/e2ee_crypto.dart": (
        "HKDF salt/info bytes: changing them makes every stored key wrap undecryptable",
        r"'viewtrip-e2ee/[a-z-]+/v1'"),
    "flutter_client/test/crypto/e2ee_frozen_vectors_test.dart": (
        "pins those frozen HKDF strings", r"viewtrip-e2ee/"),
    "spike/e2ee_spike/lib/e2ee_spike.dart": (
        "the spike must stay byte-compatible with the production HKDF strings", r"viewtrip-e2ee/"),
    "flutter_client/lib/src/auth/auth_service.dart": (
        "one-time migration of the old token key keeps users signed in", r"_legacyTokenKey = 'viewtrip_jwt'"),
    "flutter_client/test/auth/auth_token_key_test.dart": (
        "tests that migration", r"_legacyTokenKey = 'viewtrip_jwt'"),
    "flutter_client/test/brand/no_legacy_brand_test.dart": (
        "the Dart-side allowlist itself", r"."),
    "models/db_url.py": (
        "refuses to start next to an old viewtripweb.db", r"viewtripweb(\.db|_\*|\b)"),
    "tests/test_db_url.py": ("tests that guard", r"viewtripweb"),
    ".env.example": ("explains that guard", r"viewtripweb\.db"),
    "README.md": ("explains that guard", r"viewtripweb\.db"),
    ".gitignore": ("developers keep old local DB files until they rename them", r"viewtrip"),
    "api/project_transfer.py": (
        "explains why import rejects old formats", r"\.viewtrip or"),
    "tests/test_project_file_contract.py": (
        "asserts the old formats and export route are gone", r"\.viewtrip|export-viewtrip"),
    "flutter_client/test/projects/project_file_test.dart": (
        "asserts the picker refuses old-format files", r"Summer\.viewtrip"),
    "tests/test_spa_catch_all_api_404.py": (
        "names the removed export route", r"export-viewtrip"),
    "docs/ANDROID.md": ("a Google Cloud project id can never be renamed", r"id `viewtrip`"),
    "docs/DEPLOYMENT_VPS.md": (
        "pre-rename host layout and the NAS -> VPS migration history",
        r"/opt/viewtrip|/volume2/docker/viewtrip|`viewtripweb`|viewtripweb\.db"),
    "docs/RENAME_TRAXJOURNEY_RUNBOOK.md": ("the cut-over runbook names the old layout", r"."),
    "graphify-out/GRAPH_REPORT.md": ("generated; regenerated after the local folder rename", r"."),
    "tests/test_no_legacy_brand.py": ("this file", r"."),
}


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _unexpected(files: list[str], root: Path) -> list[str]:
    found = []
    for rel in files:
        if OLD_NAME.search(rel) and rel not in ALLOWED:
            found.append(f"{rel}: path")
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        allowed = re.compile(ALLOWED[rel][1]) if rel in ALLOWED else None
        for n, line in enumerate(text.splitlines(), 1):
            if OLD_NAME.search(line) and not (allowed and allowed.search(line)):
                found.append(f"{rel}:{n}: {line.strip()[:120]}")
    return found


def test_no_tracked_file_carries_the_old_name():
    found = _unexpected(_tracked_files(), ROOT)
    assert not found, "old product name still present:\n" + "\n".join(found)


def test_every_allowlist_entry_is_still_needed():
    """A stale entry would silently allow the old name back into that file."""
    files = set(_tracked_files())
    stale = []
    for rel in ALLOWED:
        path = ROOT / rel
        if rel not in files or not path.is_file():
            stale.append(f"{rel}: not tracked")
        elif not OLD_NAME.search(path.read_bytes().decode("utf-8", errors="ignore")):
            stale.append(f"{rel}: no longer mentions the old name")
    assert not stale, "remove from ALLOWED:\n" + "\n".join(stale)


def test_scanner_flags_an_unlisted_mention(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nTITLE = 'ViewTrip'\n")
    (tmp_path / "view_trip.txt").write_text("fine\n")
    (tmp_path / "README.md").write_text("see viewtripweb.db\nnew View Trip copy\n")

    found = _unexpected(["a.py", "view_trip.txt", "README.md"], tmp_path)

    assert found == ["a.py:2: TITLE = 'ViewTrip'", "view_trip.txt: path",
                     "README.md:2: new View Trip copy"]
