"""Every environment variable the server reads is documented in .env.example (#632).

.env.example is how an operator learns what a stack can be configured with, so
a variable the server reads but the file leaves out is a setting nobody finds.
LOG_LEVEL went missing that way.

The scan covers literal reads (``os.environ.get("X")``, ``os.getenv("X")``,
``os.environ["X"]``) and the module constants that name one (``_X_ENV = "X"``).
Names built at runtime, such as ``STRIPE_PRICE_{plan}``, are out of its reach.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIRS = ("api", "src", "models")
ENV_EXAMPLE = ROOT / ".env.example"

# Read by the server, but not something an operator sets.
NOT_OPERATOR_SETTINGS = {
    # Baked into the image from the release tag (Dockerfile ARG/ENV).
    "APP_VERSION",
}

_LITERAL_READ = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
)
_ENV_CONSTANT = re.compile(
    r"""^\s*_?[A-Z0-9_]*ENV[A-Z0-9_]*\s*(?::\s*\w+\s*)?=\s*["']([A-Z][A-Z0-9_]*)["']""",
    re.M,
)
_DOCUMENTED = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.M)


def _read_by_server() -> set[str]:
    names: set[str] = set()
    for d in SERVER_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in _LITERAL_READ.finditer(text):
                names.add(m.group(1) or m.group(2))
            names.update(_ENV_CONSTANT.findall(text))
    return names


def _documented() -> set[str]:
    return set(_DOCUMENTED.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_scan_sees_the_server_settings():
    # Guards the scanner itself: if its patterns stop matching, the drift
    # test below passes vacuously.
    assert {"JWT_SECRET", "LOG_LEVEL", "TRAXJOURNEY_ROLE"} <= _read_by_server()


def test_every_server_env_var_is_in_env_example():
    missing = _read_by_server() - _documented() - NOT_OPERATOR_SETTINGS
    assert not missing, (
        f"read by the server but missing from .env.example: {sorted(missing)}. "
        "Document each one there (or, if no operator ever sets it, add it to "
        "NOT_OPERATOR_SETTINGS with the reason)."
    )


def test_log_level_entry_lists_the_accepted_values():
    from src.utils.logging import LEVEL_NAMES

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    block = text[:text.index("\nLOG_LEVEL=")].rsplit("\n\n", 1)[-1]
    for name in LEVEL_NAMES:
        assert name in block
    assert "Default INFO" in block
