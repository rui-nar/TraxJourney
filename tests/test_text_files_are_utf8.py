"""Tracked text files must be valid UTF-8.

``docs/DEPLOYMENT_VPS.md`` carried a lone Windows-1252 em dash (byte 0x97) for
months: GitHub rendered it as "�", and any tool reading the file as UTF-8 —
Python's ``open()``, a docs build — stopped with a UnicodeDecodeError. Editors
on Windows can still write it, so this pins the encoding.

Runs where git and a checkout exist (CI, a dev checkout) and skips elsewhere.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TEXT = re.compile(
    r"\.(md|txt|py|ps1|bat|sh|ya?ml|toml|ini|cfg|conf|river|example|service|dart|html|js|json|jinja2|arb)$"
    r"|(^|/)(Dockerfile|Caddyfile)$"
)


def _tracked_text_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p and _TEXT.search(p)]


def _not_utf8(files: list[str], root: Path) -> list[str]:
    bad = []
    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            bad.append(f"{rel}: byte 0x{e.object[e.start]:02x} at offset {e.start}")
    return bad


def test_tracked_text_files_are_utf8():
    bad = _not_utf8(_tracked_text_files(), ROOT)
    assert not bad, "re-save as UTF-8:\n" + "\n".join(bad)


def test_detects_a_windows_1252_dash(tmp_path):
    (tmp_path / "ok.md").write_text("a — b\n", encoding="utf-8")
    (tmp_path / "bad.md").write_bytes(b"a \x97 b\n")

    assert _not_utf8(["ok.md", "bad.md"], tmp_path) == ["bad.md: byte 0x97 at offset 2"]
