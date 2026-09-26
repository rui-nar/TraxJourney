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
    "tests/test_safe_fetch.py": "public vs private address rules need public addresses; "
                                "resolution and connections are stubbed, nothing connects",
    "tests/test_immich_api.py": "a stubbed resolver maps example names to a public address; "
                                "nothing connects",
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


_DEPLOY_DOC = ROOT / "docs" / "DEPLOYMENT_VPS.md"
# Commands that take an account name, and where the name sits in each. Only
# these count: `-o` is an account for `install`, an output file for `curl`.
_NAME = r"([^\s;`)|&:\"'-][^\s;`)|&:\"']*)"   # a token that is not an option
_ACCOUNT_ARGS = [
    re.compile(r"\bsudo\s+(?:-[A-Za-tv-z]+\s+)*-[A-Za-z]*u\s*" + _NAME),   # -u x, -Hu x, -iu x
    re.compile(r"\bsudo\s+(?:-\S+\s+)*--user[=\s]" + _NAME),
    re.compile(r"(?:^|[;&|`(\"'])\s*id\s+" + _NAME),                      # a command, not the word
    re.compile(r"\bUser=([a-z_][\w.-]*)"),                                # systemd; not "`User=` in"
    re.compile(r"\bchown\s+(?:--?[\w-]+(?:=\S+)?\s+)*" + _NAME),
    re.compile(r"\badduser\s+(?:--?\S+\s+)*" + _NAME),
    re.compile(r"(?<![\w.-])([a-z_][\w.-]*)@(?:<[\w-]+>|[\w-]+(?:\.[\w-]+)+)"),  # user@host
    re.compile(r"/home/([^\s/;`)|&:\"'-][^\s/;`)|&:\"']*)"),
    re.compile(r"\bDEPLOY_USER=[\"']?([a-z_][\w.-]*)"),
]
# usermod's account is its last argument, whatever order the options come in.
_USERMOD = re.compile(r"\busermod\s+([^;|&#\n]+)")
_INSTALL_OWNER = re.compile(r"\s-[og]\s+(\S+)")
# Placeholders, shell variables, and accounts every host has. `git@github.com`
# is GitHub's SSH login, not the operator's.
_NOT_AN_ACCOUNT = re.compile(r"^(<.*|\$.*|root|git)$")


def _named_users(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        names = [m.group(1) for rx in _ACCOUNT_ARGS for m in rx.finditer(line)]
        for m in _USERMOD.finditer(line):
            names += m.group(1).split()[-1:]
        if re.search(r"\binstall\b", line):
            names += _INSTALL_OWNER.findall(line)
        found += [f"{line.strip()} -> {n}" for n in (n.strip("`\"'") for n in names)
                  if not _NOT_AN_ACCOUNT.match(n)]
    return found


def test_deployment_doc_names_no_real_login_user():
    """#444: the doc gave the deploy user as `debian`, and §8 as the owner's own
    account. Both are the operator's, like the host: docs say <deploy-user>."""
    doc = _DEPLOY_DOC.read_text(encoding="utf-8")
    row = re.search(r"^\| SSH user \| (.+) \|$", doc, re.MULTILINE)
    assert row and row.group(1).startswith("`<deploy-user>`"), row and row.group(1)
    assert _named_users(doc) == []


def test_tracked_deploy_config_names_no_real_login_user():
    """The files installed on the VPS as they are, e.g. the webhook unit's User=,
    and the template deploy.env is made from."""
    config = [p for p in _tracked_files() if p.startswith("vps/") or p == "deploy.env.example"]
    assert {"vps/webhook/webhook.service", "deploy.env.example"} <= set(config)
    found = [f"{p}: {hit}" for p in config for hit in _named_users((ROOT / p).read_text(encoding="utf-8"))]
    assert found == [], "use <deploy-user>; the install step fills it in"


@pytest.mark.parametrize(("line", "named"), [
    # Missed in round 2 of the #444 review: planted names that got through.
    ("set User=someone in the copy", True),
    ("sudo usermod -a -G docker someone", True),
    ("sudo usermod -G docker -a someone", True),
    ("sudo -Hu someone docker ps", True),
    ("sudo -iu someone", True),
    ("sudo --user=someone docker ps", True),
    ('ssh -i key <deploy-user>@<vps-host> "id someone; docker ps"', True),
    ("sudo chown --recursive someone /opt/x", True),
    ("ssh -i key someone@<vps-host>", True),
    ("scp key.pub someone@vps.example.invalid:", True),
    ("cat /home/someone/.ssh/authorized_keys", True),
    ("DEPLOY_USER=someone", True),
    ('DEPLOY_USER="someone"', True),
    # ... and what must stay quiet.
    ("the validation webhook runs as it (`User=` in the installed unit)", False),
    ("ssh -T git@github.com", False),
    ("DEPLOY_USER=", False),
    ("cat /home/<deploy-user>/.ssh/authorized_keys", False),
    ("ls /home/$USER", False),
    ("sudo usermod -aG docker,sudo <deploy-user>", False),
    ("sudo -u someone -H docker ps", True),
    ("id someone", True),
    ("  id someone   # groups", True),
    ("check with `id someone`", True),
    ("sudo install -d -o someone -g someone -m 755 /x", True),
    ("sudo install -d -m 755 -g someone /x", True),
    ("User=someone", True),
    ("sudo chown -R someone:someone /opt/x", True),
    ("sudo adduser someone", True),
    ("sudo usermod -aG docker,sudo someone", True),
    ("sudo -u <deploy-user> -H docker ps", False),
    ("sudo install -d -m 700 -o <deploy-user> -g <deploy-user> /x", False),
    ("ssh -i key <deploy-user>@<vps-host> \"id; docker ps\"", False),
    ("sudo usermod -aG docker,sudo <deploy-user>", False),
    ("sudo chown -R $USER:$USER /opt/app", False),
    ("a line that ends in `sudo chown -R", False),
    ('sed "s/^User=<deploy-user>$/User=$(id -un)/" webhook.service', False),
    # Not a command naming an account (review of #444).
    ("curl -o out.txt https://example.invalid/x", False),
    ('curl -fsSLo "$f" "https://example.invalid/$f"', False),
    ("the id of the trip is kept", False),
    ("run it as `sudo -u root` if you must", False),
    ("tar -g snapshot.file -cf x.tar /x", False),
])
def test_named_user_scanner(line, named):
    """Every positive plants `someone`: finding some other token is a miss."""
    names = [hit.rsplit(" -> ", 1)[1] for hit in _named_users(line)]
    assert set(names) == ({"someone"} if named else set()), names
