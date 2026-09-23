#!/usr/bin/env python3
"""Check that a deploy actually took effect (issue #423).

deploy.ps1 used to print "Deployed" once ``docker compose down / pull / up -d``
exited 0. None of those prove the new version is being served:

* ``docker compose pull`` pulls whatever ``image:`` the HOST's compose file
  names, not the image the script believes it is deploying. A host compose file
  left on an old image name keeps pulling that forever, and every deploy
  "succeeds".
* A container that starts and then crash-loops counts as "up".

This module is the decision half of the fix. deploy.ps1 does the deploying and
calls it twice:

``preflight`` (before anything is built or taken down)
    Reads ``docker compose config --images`` on the host and refuses to go on
    unless the compose file uses the image being deployed. Works out which
    version the server should report once the deploy is done, and records the
    version it reports now.

``verify`` (right after ``up -d``)
    Polls ``<url>/api/version`` until it reports that version, checks every
    compose service is running (and healthy, where it has a healthcheck) and
    has not restarted, and checks the app containers run the image that was
    just pulled. Prints a summary and exits non-zero on any failure.

Everything here only reads: ``docker compose config``/``ps``, ``docker
inspect`` and an HTTP GET. It is stdlib-only so any Python 3 can run it, and
the SSH runner, HTTP fetch, clock and sleep are injectable so the logic is
tested without a host (tests/test_deploy_verify.py).
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

# The server's /api/version default when no APP_VERSION was baked in.
DEV_VERSION = "dev"

VERSION_TIMEOUT = 120.0  # seconds to wait for /api/version to report the new version
VERSION_INTERVAL = 3.0
SETTLE_SECONDS = 15.0    # minimum time after `up -d` before container state is trusted
SERVICES_TIMEOUT = 60.0  # how long a healthcheck may stay "starting" after that
SERVICES_INTERVAL = 5.0

RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# CI stamps a :validation image "validation-<git rev-parse --short>" (docker-build.yml).
VALIDATION_PREFIX = "validation-"
MIN_SHORT_SHA = 7


# ── Image references ──────────────────────────────────────────────────────────

def normalize_ref(ref: str) -> str:
    """``repo`` and ``repo:latest`` name the same image; compare them as one."""
    ref = ref.strip()
    if "@" in ref or ":" in ref.rsplit("/", 1)[-1]:
        return ref
    return f"{ref}:latest"


def repository(ref: str) -> str:
    """The reference without its tag or digest."""
    ref = normalize_ref(ref)
    if "@" in ref:
        return ref.split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    name = last.rsplit(":", 1)[0]
    return f"{head}/{name}" if head else name


def namespace(ref: str) -> str:
    """Registry and owner, e.g. ``ghcr.io/rui-nar``; empty for ``redis:7``."""
    repo = repository(ref)
    return repo.rsplit("/", 1)[0] if "/" in repo else ""


def is_related(ref: str, expected: str) -> bool:
    """Same repository, or another image from the same registry owner.

    The second half is what catches a host compose file still on a
    pre-rename image name: a different repository, but ours.
    """
    if repository(ref) == repository(expected):
        return True
    owner = namespace(expected)
    return bool(owner) and namespace(ref) == owner


# ── (a) The host's compose file names the image being deployed ────────────────

def check_compose_images(compose_images: str, expected: str, directory: str) -> list[str]:
    """Problems with the image list printed by ``docker compose config --images``."""
    expected = normalize_ref(expected)
    refs = sorted({normalize_ref(line) for line in compose_images.splitlines() if line.strip()})
    compose_file = f"{directory.rstrip('/')}/docker-compose.yml"
    if expected not in refs:
        return [
            f"{compose_file} does not use {expected}; it names {', '.join(refs) or 'no images'}. "
            f"`docker compose pull` pulls what that file names, so the host would keep "
            f"running the old image. Point its image: lines at {expected}."
        ]
    problems = []
    for ref in refs:
        if ref != expected and is_related(ref, expected):
            problems.append(
                f"{compose_file} also names {ref}. Every app service must use {expected}; "
                f"a service left on another image runs code this deploy does not update."
            )
    return problems


# ── (c) Every service is running ──────────────────────────────────────────────

def parse_compose_ps(text: str) -> list[dict]:
    """``docker compose ps --format json``: one object per line since Compose
    2.21, a single JSON array before that. Accept both."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def parse_json_list(text: str) -> list[dict]:
    """``docker inspect`` / ``docker image inspect`` output (a JSON array)."""
    text = text.strip()
    return json.loads(text) if text else []


def restart_counts(inspect: list[dict]) -> dict[str, int]:
    return {c["Name"].lstrip("/"): int(c.get("RestartCount", 0)) for c in inspect}


@dataclass
class ServiceReport:
    problems: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)


def check_services(services: list[str], ps: list[dict], restarts: dict[str, int]) -> ServiceReport:
    """Hard failures, still-settling containers, and one state line per container.

    The deploy runs ``down`` before ``up -d``, so every container is new and
    its restart count starts at 0. A container caught between crashes reports
    "running", which is why the restart count is checked as well as the state.
    """
    report = ServiceReport()
    seen = {entry.get("Service") for entry in ps}
    for service in services:
        if service not in seen:
            report.problems.append(f"service {service} has no container")

    for entry in sorted(ps, key=lambda e: (e.get("Service", ""), e.get("Name", ""))):
        name = entry.get("Name", "?")
        state = entry.get("State", "")
        health = entry.get("Health", "")
        status = entry.get("Status", state)
        count = restarts.get(name)
        report.states.append(f"{name}: {status}" + (f", restarted {count}x" if count else ""))

        if state == "restarting":
            report.problems.append(f"{name} is crash-looping ({status})")
        elif state in ("exited", "dead"):
            report.problems.append(f"{name} is not running ({status})")
        elif state != "running":
            report.pending.append(f"{name} is {state or 'in an unknown state'} ({status})")
        elif health == "unhealthy":
            report.problems.append(f"{name} is unhealthy ({status})")
        elif health == "starting":
            report.pending.append(f"{name} healthcheck is still starting ({status})")

        if count is None:
            report.problems.append(f"{name} could not be inspected for restarts")
        elif count > 0 and state != "restarting":
            report.problems.append(
                f"{name} has restarted {count} time(s) since `up -d` created it, so it is crashing"
            )
    return report


# ── (b) The app containers run the image just pulled ──────────────────────────

def short_id(image_id: str) -> str:
    return image_id.split(":", 1)[-1][:12]


def check_running_image(inspect: list[dict], images: list[dict], expected: str) -> list[str]:
    """Problems with which image the app containers run.

    ``images`` is ``docker image inspect <expected>`` on the host after the pull:
    its Id is what the tag resolves to now, and every container created from
    the tag must be running exactly that.
    """
    expected = normalize_ref(expected)
    if not images:
        return [f"{expected} is not present on the host after the pull"]
    pulled = images[0]["Id"]
    problems = []
    app = [c for c in inspect if normalize_ref(c["Config"]["Image"]) == expected]
    if not app:
        problems.append(f"no container runs {expected}")
    for c in inspect:
        name = c["Name"].lstrip("/")
        ref = normalize_ref(c["Config"]["Image"])
        if ref == expected and c["Image"] != pulled:
            problems.append(
                f"{name} runs image {short_id(c['Image'])}, not {short_id(pulled)} "
                f"that {expected} was just pulled as"
            )
        elif ref != expected and is_related(ref, expected):
            problems.append(f"{name} runs {ref}, not {expected}")
    return problems


def digest_line(images: list[dict], expected: str) -> str:
    if not images:
        return "(not on host)"
    digests = [d for d in images[0].get("RepoDigests") or [] if d.startswith(repository(expected) + "@")]
    return f"{digests[0] if digests else '(no registry digest)'}  id {short_id(images[0]['Id'])}"


# ── (d) The server reports the version being deployed ─────────────────────────

@dataclass(frozen=True)
class Expectation:
    """What /api/version must report once the deploy is live.

    kind ``exact``   value is the version string.
    kind ``commit``  value is the full sha the ``validation`` tag points at; CI
                     stamps ``validation-<short sha>``, and the short sha's
                     length depends on the clone, so a prefix is compared.
    kind ``changed`` the version could not be worked out; value is what the
                     server reported before the deploy, and anything else
                     counts. With no earlier value (""), any answer counts:
                     fetch_version never returns an empty version.
    """

    kind: str
    value: str
    reason: str

    def matches(self, served: str) -> bool:
        if self.kind == "exact":
            return served == self.value
        if self.kind == "commit":
            if not served.startswith(VALIDATION_PREFIX):
                return False
            sha = served[len(VALIDATION_PREFIX):]
            return len(sha) >= MIN_SHORT_SHA and self.value.lower().startswith(sha.lower())
        return served != self.value  # "changed"

    def describe(self) -> str:
        if self.kind == "exact":
            return self.value
        if self.kind == "commit":
            return f"{VALIDATION_PREFIX}{self.value[:MIN_SHORT_SHA]}"
        return f"anything but {self.value}" if self.value else "any version"


def latest_release_tag(tags: list[str]) -> Optional[str]:
    """The newest vX.Y.Z tag, compared numerically (v0.10.0 is newer than v0.9.0)."""
    releases = [(tuple(int(p) for p in m.groups()), t) for t in tags for m in [RELEASE_TAG.match(t.strip())] if m]
    return max(releases)[1] if releases else None


GitRunner = Callable[[list[str]], Optional[str]]


def derive_expectation(target: str, built_version: Optional[str], git: GitRunner) -> tuple[Optional[Expectation], str]:
    """The version the deployed server should report, or None and why not.

    A local build bakes its own version in. Otherwise the image was built by
    docker-build.yml, which stamps :validation with ``validation-<short sha>``
    of the commit the ``validation`` tag points at, and :latest with the
    release tag name. deploy.ps1 force-fetches tags first, so the local tags
    match what CI built from.
    """
    if built_version:
        if built_version == DEV_VERSION:
            return None, (f"the build has no git tag to describe, so the server reports "
                          f"'{DEV_VERSION}', the same as any untagged build")
        return Expectation("exact", built_version, "the version baked into this build"), ""
    if target == "validation":
        sha = git(["rev-parse", "validation^{commit}"])
        if not sha:
            return None, "there is no local `validation` tag to read the commit from"
        return Expectation("commit", sha.strip(), "CI stamps :validation with the commit the `validation` tag points at"), ""
    latest = latest_release_tag((git(["tag", "--list", "v*"]) or "").splitlines())
    if not latest:
        return None, "there is no vX.Y.Z release tag to read the version from"
    return Expectation("exact", latest, "CI stamps :latest with the newest release tag"), ""


@dataclass
class VersionResult:
    ok: bool
    served: Optional[str]
    error: Optional[str]


def fetch_version(url: str, timeout: float = 10.0) -> str:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/version",
        headers={"Cache-Control": "no-cache", "User-Agent": "traxjourney-deploy-verify"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        version = json.load(response).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"/api/version answered without a version: {version!r}")
    return version


FETCH_ERRORS = (OSError, ValueError, http.client.HTTPException)


def wait_for_version(
    fetch: Callable[[], str],
    expectation: Expectation,
    timeout: float = VERSION_TIMEOUT,
    interval: float = VERSION_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> VersionResult:
    """Poll until the served version matches, or ``timeout`` runs out.

    Errors count as "not yet": the site answers 502 while the API boots and
    runs its migrations.
    """
    deadline = clock() + timeout
    served: Optional[str] = None
    error: Optional[str] = None
    reported = None
    while True:
        try:
            served, error = fetch(), None
        except FETCH_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"
        if served is not None and error is None and expectation.matches(served):
            return VersionResult(True, served, None)
        now_seeing = error or served
        if now_seeing != reported:
            log(f"  /api/version: {'no answer (' + error + ')' if error else served}; waiting for {expectation.describe()}")
            reported = now_seeing
        if clock() + interval > deadline:
            return VersionResult(False, served, error)
        sleep(interval)


# ── Talking to the host ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Host:
    host: str
    port: int
    user: str
    key: str
    directory: str

    def ssh_argv(self, command: str) -> list[str]:
        return ["ssh", "-i", self.key, "-p", str(self.port), f"{self.user}@{self.host}", command]


def run_ssh(host: Host, command: str) -> tuple[int, str]:
    """Run a read-only command on the host; stderr goes straight to the console."""
    done = subprocess.run(host.ssh_argv(command), stdout=subprocess.PIPE, text=True, encoding="utf-8")
    return done.returncode, done.stdout


Runner = Callable[[Host, str], tuple[int, str]]


def remote(host: Host, *commands: str) -> str:
    """One shell line: cd into the compose directory, then run ``commands``.
    Single-quoted only, so nothing is mangled by Windows argument quoting."""
    return "; ".join([f"cd {shlex.quote(host.directory)} || exit 1", *commands])


def compose_images_command(host: Host) -> str:
    return remote(host, "docker compose config --images")


def host_state_command(host: Host, image: str) -> str:
    return remote(
        host,
        "set -e",
        "echo '### ps'",
        "docker compose ps --all --format json",
        "echo '### services'",
        "docker compose config --services",
        "echo '### inspect'",
        "docker compose ps --all -q | xargs -r docker inspect",
        "echo '### image'",
        f"docker image inspect {shlex.quote(image)} 2>/dev/null || true",
    )


def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


@dataclass
class HostReport:
    problems: list[str]
    states: list[str]
    image: str


def read_host(host: Host, image: str, runner: Runner) -> tuple[HostReport, list[str]]:
    """One snapshot of the host: (report, still-settling containers)."""
    code, out = runner(host, host_state_command(host, image))
    if code != 0:
        return HostReport([f"could not read container state from the host (ssh exit {code})"], [], ""), []
    try:
        sections = split_sections(out)
        ps = parse_compose_ps(sections["ps"])
        services = [s for s in sections["services"].splitlines() if s.strip()]
        inspect = parse_json_list(sections["inspect"])
        images = parse_json_list(sections["image"])
    except (KeyError, ValueError) as exc:
        return HostReport([f"could not parse the host's container state: {exc}"], [], ""), []
    services_report = check_services(services, ps, restart_counts(inspect))
    problems = services_report.problems + check_running_image(inspect, images, image)
    return HostReport(problems, services_report.states, digest_line(images, image)), services_report.pending


def wait_for_host(
    snapshot: Callable[[], tuple[HostReport, list[str]]],
    started: float,
    settle: float = SETTLE_SECONDS,
    timeout: float = SERVICES_TIMEOUT,
    interval: float = SERVICES_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HostReport:
    """Snapshot the host no sooner than ``settle`` seconds after ``started``.

    A crash-looping container needs a moment to crash and be restarted before
    it shows. Hard failures return at once; containers still starting are
    re-read until ``timeout`` after the first snapshot, then count as failures.
    """
    wait = started + settle - clock()
    if wait > 0:
        sleep(wait)
    deadline = clock() + timeout
    while True:
        report, pending = snapshot()
        if report.problems or not pending:
            return report
        if clock() + interval > deadline:
            report.problems.extend(f"{p}, still not settled after {timeout:.0f}s" for p in pending)
            return report
        sleep(interval)


# ── Commands ──────────────────────────────────────────────────────────────────

def git_runner(repo: str) -> GitRunner:
    def run(args: list[str]) -> Optional[str]:
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None
    return run


def host_from(args: argparse.Namespace) -> Host:
    return Host(args.ssh_host, args.ssh_port, args.ssh_user, args.ssh_key, args.dir)


def cmd_preflight(args: argparse.Namespace, runner: Runner = run_ssh,
                  fetch: Callable[[str], str] = fetch_version, git: Optional[GitRunner] = None) -> int:
    host = host_from(args)
    code, out = runner(host, compose_images_command(host))
    if code != 0:
        print(f"FAIL: could not read the compose file in {host.directory} on the host (ssh exit {code})")
        return 1
    problems = check_compose_images(out, args.image, host.directory)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print(f"  host compose uses {normalize_ref(args.image)}")

    expectation, why_not = derive_expectation(args.target, args.built_version, git or git_runner(args.repo))
    try:
        previous = fetch(args.url)
    except FETCH_ERRORS as exc:
        previous = ""
        print(f"  {args.url} does not answer /api/version right now ({type(exc).__name__})")
    if expectation is None:
        expectation = Expectation("changed", previous, f"the exact version is unknown: {why_not}")
        print(f"WARNING: cannot tell which version this deploy should serve: {why_not}.")
        print(f"         Verification will only require the version to change from "
              f"'{previous or 'nothing'}', which does not prove it is the right one.")
    print(f"  serving now: {previous or '(no answer)'}; expecting after deploy: "
          f"{expectation.describe()} ({expectation.reason})")
    with open(args.state, "w", encoding="utf-8") as fh:
        json.dump({"expectation": asdict(expectation), "previous": previous}, fh)
    return 0


def cmd_verify(args: argparse.Namespace, runner: Runner = run_ssh,
               fetch: Callable[[str], str] = fetch_version,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> int:
    started = clock()
    host = host_from(args)
    with open(args.state, encoding="utf-8") as fh:
        state = json.load(fh)
    expectation = Expectation(**state["expectation"])

    version = wait_for_version(lambda: fetch(args.url), expectation, timeout=args.version_timeout,
                               clock=clock, sleep=sleep)
    report = wait_for_host(lambda: read_host(host, args.image, runner), started, clock=clock, sleep=sleep)

    problems = []
    if not version.ok:
        served = version.served if version.served is not None else "nothing"
        detail = f" (last error: {version.error})" if version.error else ""
        problems.append(
            f"{args.url}/api/version reports {served}, not {expectation.describe()}, "
            f"after {args.version_timeout:.0f}s{detail}"
        )
    problems.extend(report.problems)

    ok = "OK" if version.ok else "FAIL"
    print("")
    print("Deploy verification")
    print(f"  version   expected {expectation.describe()}  ({expectation.reason})")
    print(f"            before   {state['previous'] or '(no answer)'}")
    print(f"            served   {version.served or '(no answer)'}  {ok}")
    if expectation.kind == "changed":
        print("            WARNING: only a change of version was checked, not the exact version")
    print(f"  image     {normalize_ref(args.image)}")
    print(f"            {report.image}")
    print("  services")
    for line in report.states:
        print(f"            {line}")
    if problems:
        print("")
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--ssh-host", required=True)
        p.add_argument("--ssh-port", type=int, required=True)
        p.add_argument("--ssh-user", required=True)
        p.add_argument("--ssh-key", required=True)
        p.add_argument("--dir", required=True, help="compose directory on the host")
        p.add_argument("--image", required=True, help="image reference being deployed, with its tag")
        p.add_argument("--url", required=True, help="base URL the deployed environment serves")
        p.add_argument("--state", required=True, help="file preflight writes and verify reads")
        if name == "preflight":
            p.add_argument("--target", choices=("validation", "prod"), required=True)
            p.add_argument("--built-version", help="APP_VERSION baked into a local build")
            p.add_argument("--repo", default=".", help="git checkout to read tags from")
        else:
            p.add_argument("--version-timeout", type=float, default=VERSION_TIMEOUT)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return cmd_preflight(args) if args.command == "preflight" else cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
