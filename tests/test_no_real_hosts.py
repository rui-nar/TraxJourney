"""Tracked files must not publish the operator's real hosts (issue #424).

The repository is public. ``docs/DEPLOYMENT_VPS.md`` once carried
copy-pasteable commands with the NAS's dynamic-DNS hostname, its non-default
SSH port and the login user — a ready-made SSH target. Operator-specific
values belong in gitignored config; docs use ``<placeholders>``.

Flags, in tracked text files:

- public IPv4 addresses (private, loopback, CGNAT/Tailscale and documentation
  ranges are fine);
- dynamic-DNS hostnames (``*.synology.me``, ``*.duckdns.org``, ...);
- ``ssh``/``scp``/``rsync`` commands aimed at a concrete ``user@host`` on a
  non-default port.

Runs where git and a checkout exist (CI, a dev checkout) and skips elsewhere.
"""

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Text we scan. Lock files, generated output and binary assets are skipped.
_SCANNED = re.compile(
    r"\.(md|txt|py|ps1|bat|sh|ya?ml|toml|ini|cfg|conf|river|example|service|dart|html|js|json)$"
    r"|(^|/)(Dockerfile|Caddyfile)$"
)
# This file's own examples look like the real thing on purpose.
_SKIPPED = re.compile(r"(^|/)(graphify-out|build)/|\.lock$|^tests/test_no_real_hosts\.py$")

_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
_DDNS = re.compile(
    r"[\w-]+\.(synology\.me|duckdns\.org|dyndns\.org|ddns\.net|no-ip\.(?:org|com)|myqnapcloud\.com|freeboxos\.fr)\b",
    re.IGNORECASE,
)
# ssh -p 2222 user@host / scp -P 2222 user@host: / rsync -e "ssh -p 2222" user@host:
_SSH_TARGET = re.compile(
    r"\b(?:ssh|scp|rsync)\b[^\n]*?-[pP]\s*\"?(?!22\b)\d{2,5}\b[^\n]*?\s[A-Za-z][\w.-]*@[A-Za-z0-9][\w.-]*"
)

# path -> why a public address in it is fine
ALLOWED_IPS: dict[str, str] = {
    "src/services/overpass_service.py": "public Overpass API mirrors, logged in a comment",
}


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _findings(files: list[str], root: Path) -> list[str]:
    found = []
    for rel in files:
        if not _SCANNED.search(rel) or _SKIPPED.search(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            where = f"{rel}:{n}"
            if rel not in ALLOWED_IPS:
                for candidate in _IPV4.findall(line):
                    try:
                        ip = ipaddress.IPv4Address(candidate)
                    except ipaddress.AddressValueError:
                        continue  # 999.1.2.3, or a version string
                    if ip.is_global:
                        found.append(f"{where}: public IP {candidate}")
            for m in _DDNS.finditer(line):
                found.append(f"{where}: dynamic-DNS host {m.group(0)}")
            if _SSH_TARGET.search(line):
                found.append(f"{where}: ssh target with a concrete user@host and port")
    return found


def test_tracked_files_publish_no_real_hosts():
    found = _findings(_tracked_files(), ROOT)
    assert not found, (
        "use <placeholders> or gitignored config instead:\n" + "\n".join(found)
    )


def test_every_ip_allowlist_entry_is_still_needed():
    stale = []
    for rel in ALLOWED_IPS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if not any(
            ipaddress.IPv4Address(c).is_global
            for c in _IPV4.findall(text)
            if all(int(o) <= 255 for o in c.split("."))
        ):
            stale.append(rel)
    assert not stale, f"remove from ALLOWED_IPS: {stale}"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ('ssh -p 2222 someone@nas.example.net "ls"', True),
        ("scp -O -P 2222 someone@203.0.114.9:/x /y", True),
        ('rsync -avz -e "ssh -p 2222" someone@nas.example.net:/a/ /b/', True),
        ("ssh -p <nas-ssh-port> <nas-user>@<nas-host>", False),
        ("ssh -p 22 deploy@server", False),
        ("see home.synology.me for the NAS", True),
        ("VPS at 93.184.216.34", True),
        ("bind 127.0.0.1, LAN 192.168.1.20, tailnet 100.101.102.103", False),
        ("docs example 203.0.113.7, version 3.47.1", False),
    ],
)
def test_scanner(tmp_path, line, flagged):
    (tmp_path / "doc.md").write_text(line + "\n")
    assert bool(_findings(["doc.md"], tmp_path)) is flagged
