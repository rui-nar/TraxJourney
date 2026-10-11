"""Every web build serves its own engine files, not Google's (#601, #602).

By default ``flutter build web`` makes the client load CanvasKit from
www.gstatic.com, and the web engine fetches its fallback fonts from
fonts.gstatic.com, so every visitor's address reached Google at each start.
Both release builds (CI and deploy.ps1) pass ``--no-web-resources-cdn`` and
then mirror the fallback fonts into the build; web/flutter_bootstrap.js points
the engine at them. The privacy policy relies on it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDS = {
    "CI": ROOT / ".github" / "workflows" / "docker-build.yml",
    "deploy.ps1": ROOT / "deploy.ps1",
}


def _build_command(text: str) -> str:
    """The ``flutter build web`` command, with PowerShell continuations joined."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*flutter build web\b", line):
            command = line
            while command.rstrip().endswith("`"):
                i += 1
                command = command.rstrip()[:-1] + " " + lines[i].strip()
            return command
    raise AssertionError("no flutter build web command")


def test_builds_take_canvaskit_from_the_build():
    for name, path in BUILDS.items():
        command = _build_command(path.read_text(encoding="utf-8"))
        assert "--no-web-resources-cdn" in command, name


def test_builds_mirror_the_fallback_fonts_after_building():
    for name, path in BUILDS.items():
        text = path.read_text(encoding="utf-8")
        build = text.index("flutter build web")
        mirror = text.find("dart run tool/mirror_fallback_fonts.dart build/web")
        assert mirror > build, name
