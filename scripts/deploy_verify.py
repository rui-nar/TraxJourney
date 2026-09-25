#!/usr/bin/env python3
"""Check that a deploy actually took effect (issue #423).

deploy.ps1 used to print "Deployed" once its remote ``down``, ``pull`` and
``up -d`` exited 0. None of those prove the new version is being served:

* ``docker compose pull`` pulls whatever ``image:`` the HOST's compose file
  names, not the image the script believes it is deploying. A host compose file
  left on an old image name keeps pulling that forever, and every deploy
  "succeeds".
* A container that starts and then crash-loops counts as "up".

deploy.ps1 builds (when it builds) and then hands the rest to ``deploy``, which
does everything on the host through ONE SSH session (issue #439): a key with a
passphrase and no agent asks once per connection, and Windows' OpenSSH has no
ControlMaster to share one. In that session, in this order:

1. Host check. Reads ``docker compose config --images`` and stops unless the
   compose file uses the image being deployed. Works out which version the
   server should report once the deploy is done, and records the one it
   reports now.
2. Push (local build only). Nothing leaves the machine before step 1 passed.
3. ``docker compose pull``, then ``docker compose up -d``. No ``down``: a
   failed pull leaves the running containers alone, and ``up -d`` recreates
   only the containers whose image or configuration changed.
4. Verify. Polls ``<url>/api/version`` until it reports that version, checks
   every compose service is running (and healthy, where it has a
   healthcheck) and has not restarted during the deploy, and checks the app
   containers run the image that was just pulled.
5. Re-check. Reads the containers once more ``RECHECK_SECONDS`` later, so a
   container that crashes after the checks passed still fails the deploy.

``expect`` prints the version the deployed server should report, for the
banner deploy.ps1 shows before it starts.

Apart from the push, the pull and ``up -d``, everything here only reads:
``docker compose config``/``ps``, ``docker inspect`` and an HTTP GET. It is
stdlib-only so any Python 3 can run it, and the SSH session, push, HTTP fetch,
clock and sleep are injectable so the logic is tested without a host
(tests/test_deploy_verify.py).
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

# The server's /api/version default when no APP_VERSION was baked in.
DEV_VERSION = "dev"

VERSION_TIMEOUT = 120.0  # seconds to wait for /api/version to report the new version
VERSION_INTERVAL = 3.0
SETTLE_SECONDS = 15.0    # minimum time after `up -d` before container state is trusted
SERVICES_TIMEOUT = 60.0  # how long a healthcheck may stay "starting" after that
SERVICES_INTERVAL = 5.0
RECHECK_SECONDS = 30.0   # after everything passed, how long before the containers are read once more

# deploy's exit code when it stopped before the running containers were touched.
EXIT_UNTOUCHED = 3

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


def restart_baseline(inspect: list[dict]) -> dict[str, int]:
    """Restart count per container Id, read before the deploy changes anything."""
    return {c["Id"]: int(c.get("RestartCount", 0)) for c in inspect}


def restart_counts(inspect: list[dict], baseline: Optional[dict[str, int]] = None) -> dict[str, int]:
    """Restarts per container name since ``baseline`` was read.

    There is no ``down``, so a container ``up -d`` did not need to recreate
    keeps its Id and the restarts it had before the deploy; only the ones
    since count. A recreated container has a new Id and counts from 0.
    """
    baseline = baseline or {}
    return {c["Name"].lstrip("/"): int(c.get("RestartCount", 0)) - baseline.get(c["Id"], 0) for c in inspect}


@dataclass
class ServiceReport:
    problems: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)


def check_services(services: list[str], ps: list[dict], restarts: dict[str, int]) -> ServiceReport:
    """Hard failures, still-settling containers, and one state line per container.

    ``restarts`` counts from before the deploy (see restart_counts). A
    container caught between crashes reports "running", which is why the
    restart count is checked as well as the state.
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
                f"{name} has restarted {count} time(s) during the deploy, so it is crashing"
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
        # The one session idles through the version wait and the re-check. A
        # connection dropped silently (NAT timeout, host gone) would block
        # readline() for ever; keepalives make ssh exit 255 within a minute.
        return ["ssh", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-i", self.key, "-p", str(self.port), f"{self.user}@{self.host}", command]


class Session:
    """One shell on the host, over one SSH connection, for the whole deploy.

    Each command goes down the shell's stdin, runs in a subshell with its own
    stdin on /dev/null (so it can neither ``exit`` the shell nor read the
    commands after it), and is followed by a line holding a random marker and
    its exit status. Its stdout is everything before that line; its stderr
    goes straight to the console, which is where docker's progress shows.

    Windows' OpenSSH cannot multiplex (no ControlMaster), so holding one
    connection open is the only way to authenticate once (issue #439).
    Bytes, not text mode: a text-mode pipe on Windows turns every "\\n" into
    "\\r\\n", and the carriage return would reach the remote shell.
    """

    def __init__(self, argv: list[str], popen: Callable = subprocess.Popen):
        self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.marker = f"__deploy_verify_{secrets.token_hex(8)}__"

    def run(self, command: str) -> tuple[int, str]:
        script = f"( {command}\n) </dev/null\nprintf '\\n%s %s\\n' {self.marker} \"$?\"\n"
        try:
            self.proc.stdin.write(script.encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:
            return self._ended(), ""
        lines = []
        prefix = f"{self.marker} ".encode()
        while True:
            line = self.proc.stdout.readline()
            if not line:  # the connection ended: refused key, network, host gone
                return self._ended(), ""
            if line.startswith(prefix):
                out = b"".join(lines).decode("utf-8", "replace")
                # Drop the newline printf put before the marker.
                return int(line[len(prefix):].decode().strip() or 255), out[:-1]
            lines.append(line)

    def _ended(self) -> int:
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = 255
        return code or 255

    def close(self) -> None:
        try:
            self.proc.stdin.write(b"exit\n")
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_session(host: Host, popen: Callable = subprocess.Popen) -> Session:
    return Session(host.ssh_argv("sh"), popen=popen)


Runner = Callable[[str], tuple[int, str]]


def remote(host: Host, *commands: str) -> str:
    """One shell line: cd into the compose directory, then run ``commands``."""
    return "; ".join([f"cd {shlex.quote(host.directory)} || exit 1", *commands])


def compose_images_command(host: Host) -> str:
    return remote(host, "docker compose config --images")


def baseline_command(host: Host) -> str:
    """The containers as they are before the deploy, for their restart counts."""
    return remote(host, "docker compose ps --all -q | xargs -r docker inspect")


# stdout to stderr: compose's output is for the operator to watch, not to parse.
def pull_command(host: Host) -> str:
    return remote(host, "docker compose pull 1>&2")


def up_command(host: Host) -> str:
    return remote(host, "docker compose up -d 1>&2")


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


def read_host(host: Host, image: str, runner: Runner,
              baseline: Optional[dict[str, int]] = None) -> tuple[HostReport, list[str]]:
    """One snapshot of the host: (report, still-settling containers)."""
    code, out = runner(host_state_command(host, image))
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
    services_report = check_services(services, ps, restart_counts(inspect, baseline))
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


def docker_push(ref: str) -> int:
    return subprocess.run(["docker", "push", ref]).returncode


def host_from(args: argparse.Namespace) -> Host:
    return Host(args.ssh_host, args.ssh_port, args.ssh_user, args.ssh_key, args.dir)


def check_host(args: argparse.Namespace, host: Host, runner: Runner, fetch: Callable[[str], str],
               git: GitRunner) -> Optional[tuple[Expectation, str]]:
    """Step 1: the compose file uses the image, and which version to expect.
    Returns (expectation, version served now), or None after printing why not."""
    code, out = runner(compose_images_command(host))
    if code != 0:
        print(f"FAIL: could not read the compose file in {host.directory} on the host (ssh exit {code})")
        return None
    problems = check_compose_images(out, args.image, host.directory)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return None
    print(f"  host compose uses {normalize_ref(args.image)}")

    expectation, why_not = derive_expectation(args.target, args.built_version, git)
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
    return expectation, previous


def verify(args: argparse.Namespace, host: Host, runner: Runner, expectation: Expectation, previous: str,
           baseline: dict[str, int], started: float, fetch: Callable[[str], str],
           clock: Callable[[], float], sleep: Callable[[float], None]) -> int:
    """Steps 4 and 5. ``started`` is when ``up -d`` returned."""
    version = wait_for_version(lambda: fetch(args.url), expectation, timeout=args.version_timeout,
                               clock=clock, sleep=sleep)
    answered = clock() - started
    snapshot = lambda: read_host(host, args.image, runner, baseline)  # noqa: E731
    report = wait_for_host(snapshot, started, clock=clock, sleep=sleep)

    problems = []
    if not version.ok:
        served = version.served if version.served is not None else "nothing"
        detail = f" (last error: {version.error})" if version.error else ""
        problems.append(
            f"{args.url}/api/version reports {served}, not {expectation.describe()}, "
            f"after {args.version_timeout:.0f}s{detail}"
        )
    problems.extend(report.problems)

    # A container can pass every check and crash a little later: a job it
    # picks up, a first scheduled run, memory filling. Look once more.
    if not problems:
        sleep(RECHECK_SECONDS)
        report, pending = snapshot()
        problems.extend(f"{p} ({RECHECK_SECONDS:.0f}s after the other checks passed)"
                        for p in report.problems + pending)

    ok = "OK" if version.ok else "FAIL"
    print("")
    print("Deploy verification")
    print(f"  version   expected {expectation.describe()}  ({expectation.reason})")
    print(f"            before   {previous or '(no answer)'}")
    print(f"            served   {version.served or '(no answer)'}  {ok}"
          + (f"  ({answered:.0f}s after up -d)" if version.ok else ""))
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


def cmd_deploy(args: argparse.Namespace, session: Callable[[Host], Session] = open_session,
               fetch: Callable[[str], str] = fetch_version, git: Optional[GitRunner] = None,
               push: Callable[[str], int] = docker_push,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> int:
    """Check the host, push, pull, start and verify, over one SSH session.

    Exits EXIT_UNTOUCHED when it stopped before ``up -d``, so the running
    containers are as they were; 1 when the deploy went ahead and failed.
    """
    host = host_from(args)
    untouched = "nothing was deployed; the running containers were not touched"
    with session(host) as shell:
        checked = check_host(args, host, shell.run, fetch, git or git_runner(args.repo))
        if checked is None:
            print(f"  {untouched}")
            return EXIT_UNTOUCHED
        expectation, previous = checked

        for ref in args.push:
            print(f"  pushing {ref}...")
            code = push(ref)
            if code != 0:
                print(f"FAIL: docker push {ref} failed (exit {code}); {untouched}")
                return EXIT_UNTOUCHED

        code, out = shell.run(baseline_command(host))
        try:
            baseline = restart_baseline(parse_json_list(out)) if code == 0 else None
        except ValueError:
            baseline = None
        if baseline is None:
            print(f"FAIL: could not read the running containers (ssh exit {code}); {untouched}")
            return EXIT_UNTOUCHED

        print(f"  pulling {normalize_ref(args.image)} (the running containers keep serving meanwhile)...")
        pull_started = clock()
        code, _ = shell.run(pull_command(host))
        if code != 0:
            print(f"FAIL: `docker compose pull` failed (exit {code}); {untouched}")
            return EXIT_UNTOUCHED
        # Before #439 the stack was down for all of this.
        print(f"  pull took {clock() - pull_started:.0f}s")

        print("  starting: `docker compose up -d` recreates what changed...")
        code, _ = shell.run(up_command(host))
        started = clock()
        if code != 0:
            print(f"FAIL: `docker compose up -d` failed (exit {code})")
            return 1

        return verify(args, host, shell.run, expectation, previous, baseline, started, fetch, clock, sleep)


def cmd_expect(args: argparse.Namespace, git: Optional[GitRunner] = None) -> int:
    """Print the version the deployed server should report, for deploy.ps1's banner."""
    expectation, why_not = derive_expectation(args.target, args.built_version, git or git_runner(args.repo))
    print(expectation.describe() if expectation else f"unknown version ({why_not})")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("deploy", "expect"):
        p = sub.add_parser(name)
        p.add_argument("--target", choices=("validation", "prod"), required=True)
        p.add_argument("--built-version", help="APP_VERSION baked into a local build")
        p.add_argument("--repo", default=".", help="git checkout to read tags from")
        if name == "deploy":
            p.add_argument("--ssh-host", required=True)
            p.add_argument("--ssh-port", type=int, required=True)
            p.add_argument("--ssh-user", required=True)
            p.add_argument("--ssh-key", required=True)
            p.add_argument("--dir", required=True, help="compose directory on the host")
            p.add_argument("--image", required=True, help="image reference being deployed, with its tag")
            p.add_argument("--url", required=True, help="base URL the deployed environment serves")
            p.add_argument("--push", action="append", default=[],
                           help="local image to push once the host check passed (repeatable)")
            p.add_argument("--version-timeout", type=float, default=VERSION_TIMEOUT)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return cmd_deploy(args) if args.command == "deploy" else cmd_expect(args)


if __name__ == "__main__":
    raise SystemExit(main())
