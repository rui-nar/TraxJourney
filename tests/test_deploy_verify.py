"""deploy.ps1's post-deploy checks (issue #423), in scripts/deploy_verify.py.

deploy.ps1 used to report success once `docker compose down / pull / up -d`
exited 0. That proves neither that the host pulled the image being deployed
(`pull` follows the host's own compose file) nor that the containers stayed up.

The fixtures under tests/fixtures/deploy_verify/ are real Docker output, not
hand-written: `docker compose ps --all --format json`, `docker compose config
--services` and `docker inspect` captured from compose projects built to be
healthy, crash-looping, caught running between two crashes ("flapping"),
exited, unhealthy and still starting (Docker 29.7, Compose 5.5). Only local
paths, the image name (python:3.14-slim -> the app image, redis -> redis:7-alpine)
and, for docker inspect, keys the checks do not read were rewritten or dropped.
`config_images_example.txt` is `docker compose config --images` run on
docker-compose.yml.example.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import deploy_verify as dv

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "deploy_verify"
VAL = "ghcr.io/rui-nar/traxjourney:validation"
LATEST = "ghcr.io/rui-nar/traxjourney:latest"
VAL_DIR = "/opt/traxjourney-val"
SHA = "3186c1b5d2e0f4a1b2c3d4e5f60718293a4b5c6d"


def fixture(*parts):
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


def snapshot(scenario):
    ps = dv.parse_compose_ps(fixture(scenario, "compose_ps.ndjson"))
    inspect = dv.parse_json_list(fixture(scenario, "docker_inspect.json"))
    services = fixture(scenario, "config_services.txt").split()
    return services, ps, inspect


def pulled_image():
    return dv.parse_json_list(fixture("image_inspect.json"))


class FakeClock:
    """Time only moves when the code under test sleeps (or a fetch takes time)."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# ── image references ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, normal", [
    ("ghcr.io/rui-nar/traxjourney", LATEST),
    (LATEST, LATEST),
    ("redis:7-alpine", "redis:7-alpine"),
    ("localhost:5000/app", "localhost:5000/app:latest"),
    ("ghcr.io/rui-nar/traxjourney@sha256:abc", "ghcr.io/rui-nar/traxjourney@sha256:abc"),
])
def test_normalize_ref_adds_the_implicit_latest_tag_only(ref, normal):
    assert dv.normalize_ref(ref) == normal


def test_repository_and_namespace():
    assert dv.repository(VAL) == "ghcr.io/rui-nar/traxjourney"
    assert dv.repository("localhost:5000/app:1") == "localhost:5000/app"
    assert dv.namespace(VAL) == "ghcr.io/rui-nar"
    assert dv.namespace("redis:7-alpine") == ""


# ── (a) compose config --images ──────────────────────────────────────────────

def test_compose_file_using_the_deployed_image_passes():
    assert dv.check_compose_images(fixture("config_images_example.txt"), LATEST, "/opt/traxjourney") == []


def test_compose_file_on_another_tag_is_refused_before_deploying():
    """The example compose names :latest; a val host must name :validation."""
    problems = dv.check_compose_images(fixture("config_images_example.txt"), VAL, VAL_DIR)
    assert len(problems) == 1
    assert f"{VAL_DIR}/docker-compose.yml does not use {VAL}" in problems[0]
    assert LATEST in problems[0]


def test_compose_file_still_on_a_pre_rename_image_is_refused():
    """Same registry owner, different repository: what an un-updated host
    compose file looks like after the image rename."""
    images = "ghcr.io/rui-nar/legacy-app:latest\nredis:7-alpine\n"
    problems = dv.check_compose_images(images, LATEST, "/opt/traxjourney")
    assert len(problems) == 1
    assert "ghcr.io/rui-nar/legacy-app:latest" in problems[0]


def test_pre_rename_image_next_to_the_deployed_one_is_refused():
    images = f"{LATEST}\nghcr.io/rui-nar/legacy-app:latest\nredis:7-alpine\n"
    problems = dv.check_compose_images(images, LATEST, "/opt/traxjourney")
    assert len(problems) == 1
    assert "also names ghcr.io/rui-nar/legacy-app:latest" in problems[0]


def test_one_service_left_on_another_image_is_refused():
    images = f"{VAL}\n{VAL}\n{LATEST}\nredis:7-alpine\n"
    problems = dv.check_compose_images(images, VAL, VAL_DIR)
    assert problems == [
        f"{VAL_DIR}/docker-compose.yml also names {LATEST}. Every app service must use {VAL}; "
        "a service left on another image runs code this deploy does not update."
    ]


def test_an_untagged_image_line_counts_as_latest():
    assert dv.check_compose_images("ghcr.io/rui-nar/traxjourney\nredis:7-alpine\n", LATEST, "/opt/x") == []


def test_unrelated_images_are_not_flagged():
    images = f"{VAL}\nredis:7-alpine\ngrafana/alloy:latest\n"
    assert dv.check_compose_images(images, VAL, VAL_DIR) == []


# ── (c) services ─────────────────────────────────────────────────────────────

def test_compose_ps_parses_ndjson_and_the_older_json_array():
    ndjson = fixture("healthy", "compose_ps.ndjson")
    array = "[" + ",".join(ndjson.strip().splitlines()) + "]"
    assert dv.parse_compose_ps(ndjson) == dv.parse_compose_ps(array)
    assert len(dv.parse_compose_ps(ndjson)) == 4
    assert dv.parse_compose_ps("\n") == []


def check(scenario, services=None):
    fixture_services, ps, inspect = snapshot(scenario)
    return dv.check_services(services or fixture_services, ps, dv.restart_counts(inspect))


def test_healthy_stack_passes():
    report = check("healthy")
    assert report.problems == [] and report.pending == []
    assert "traxjourney-val-redis-1: Up 11 seconds (healthy)" in report.states
    assert len(report.states) == 4


def test_crash_looping_container_fails():
    report = check("crashloop")
    assert report.problems == ["traxjourney-val-worker-1 is crash-looping (Restarting (3) 3 seconds ago)"]


def test_container_caught_running_between_crashes_fails_on_its_restart_count():
    """compose ps says "running"; only the restart count gives it away."""
    _, ps, _ = snapshot("flapping")
    assert {e["State"] for e in ps} == {"running"}
    report = check("flapping")
    assert report.problems == [
        "traxjourney-val-worker-1 has restarted 1 time(s) during the deploy, so it is crashing"
    ]
    assert "traxjourney-val-worker-1: Up 2 seconds, restarted 1x" in report.states


def with_restarts(inspect, name, count):
    """A copy of `inspect` where container `name` has restarted `count` times."""
    inspect = json.loads(json.dumps(inspect))
    next(c for c in inspect if c["Name"] == f"/{name}")["RestartCount"] = count
    return inspect


def test_restarts_from_before_the_deploy_do_not_count():
    """Without `down`, a container whose image did not change (redis) is not
    recreated, so it keeps the restart count it had before the deploy (#439)."""
    services, ps, inspect = snapshot("healthy")
    before = with_restarts(inspect, "traxjourney-val-redis-1", 2)
    report = dv.check_services(services, ps, dv.restart_counts(before, dv.restart_baseline(before)))
    assert report.problems == []


def test_restarts_during_the_deploy_count_for_a_kept_container():
    services, ps, inspect = snapshot("healthy")
    before = with_restarts(inspect, "traxjourney-val-redis-1", 2)
    after = with_restarts(inspect, "traxjourney-val-redis-1", 3)
    report = dv.check_services(services, ps, dv.restart_counts(after, dv.restart_baseline(before)))
    assert report.problems == [
        "traxjourney-val-redis-1 has restarted 1 time(s) during the deploy, so it is crashing"
    ]


def test_a_recreated_container_counts_all_its_restarts():
    """`up -d` gives a recreated container a new Id: nothing from the old one carries over."""
    services, ps, inspect = snapshot("flapping")
    before = [dict(c, Id="0" * 64, RestartCount=5) for c in inspect]
    report = dv.check_services(services, ps, dv.restart_counts(inspect, dv.restart_baseline(before)))
    assert report.problems == [
        "traxjourney-val-worker-1 has restarted 1 time(s) during the deploy, so it is crashing"
    ]


def test_exited_container_fails():
    assert check("exited").problems == ["traxjourney-val-worker-1 is not running (Exited (1) 11 seconds ago)"]


def test_unhealthy_container_fails():
    assert check("unhealthy").problems == ["traxjourney-val-redis-1 is unhealthy (Up 11 seconds (unhealthy))"]


def test_starting_healthcheck_is_pending_not_failed():
    report = check("starting")
    assert report.problems == []
    assert report.pending == ["traxjourney-val-redis-1 healthcheck is still starting (Up 11 seconds (health: starting))"]


def test_service_without_a_container_fails():
    services, _, _ = snapshot("healthy")
    report = check("healthy", services + ["alloy"])
    assert report.problems == ["service alloy has no container"]


def test_container_missing_from_inspect_fails():
    _, ps, inspect = snapshot("healthy")
    counts = dv.restart_counts(inspect)
    del counts["traxjourney-val-worker-1"]
    report = dv.check_services(["traxjourney", "worker", "worker-poster", "redis"], ps, counts)
    assert report.problems == ["traxjourney-val-worker-1 could not be inspected for restarts"]


def test_created_but_not_started_container_is_pending():
    _, ps, inspect = snapshot("healthy")
    ps[0] = dict(ps[0], State="created", Status="Created")
    report = dv.check_services([], ps, dv.restart_counts(inspect))
    assert report.problems == []
    assert report.pending == [f"{ps[0]['Name']} is created (Created)"]


# ── (b) running image ────────────────────────────────────────────────────────

def test_app_containers_on_the_pulled_image_pass():
    _, _, inspect = snapshot("healthy")
    assert dv.check_running_image(inspect, pulled_image(), VAL) == []


def test_container_on_an_older_image_id_fails():
    """The tag moved, but a container still runs what it pointed at before."""
    _, _, inspect = snapshot("healthy")
    worker = next(c for c in inspect if c["Name"] == "/traxjourney-val-worker-1")
    worker["Image"] = "sha256:" + "0" * 64
    assert dv.check_running_image(inspect, pulled_image(), VAL) == [
        f"traxjourney-val-worker-1 runs image 000000000000, not cad9a2c87176 that {VAL} was just pulled as"
    ]


def test_no_container_on_the_deployed_image_fails():
    _, _, inspect = snapshot("healthy")
    for c in inspect:
        if c["Config"]["Image"] == VAL:
            c["Config"]["Image"] = LATEST
    problems = dv.check_running_image(inspect, pulled_image(), VAL)
    assert problems[0] == f"no container runs {VAL}"
    assert f"traxjourney-val-traxjourney-1 runs {LATEST}, not {VAL}" in problems


def test_container_on_a_pre_rename_image_fails():
    _, _, inspect = snapshot("healthy")
    worker = next(c for c in inspect if c["Name"] == "/traxjourney-val-worker-1")
    worker["Config"]["Image"] = "ghcr.io/rui-nar/legacy-app:validation"
    assert dv.check_running_image(inspect, pulled_image(), VAL) == [
        f"traxjourney-val-worker-1 runs ghcr.io/rui-nar/legacy-app:validation, not {VAL}"
    ]


def test_image_missing_on_the_host_fails():
    _, _, inspect = snapshot("healthy")
    assert dv.check_running_image(inspect, [], VAL) == [f"{VAL} is not present on the host after the pull"]


def test_digest_line_shows_the_registry_digest_and_id():
    assert dv.digest_line(pulled_image(), VAL) == (
        "ghcr.io/rui-nar/traxjourney@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
        "  id cad9a2c87176"
    )
    assert dv.digest_line([], VAL) == "(not on host)"


def test_digest_line_picks_the_deployed_repository_digest():
    images = [{"Id": "sha256:" + "a" * 64,
               "RepoDigests": ["ghcr.io/rui-nar/other@sha256:" + "b" * 64, "ghcr.io/rui-nar/traxjourney@sha256:" + "c" * 64]}]
    assert dv.digest_line(images, VAL).startswith("ghcr.io/rui-nar/traxjourney@sha256:ccc")
    images[0]["RepoDigests"] = []
    assert dv.digest_line(images, VAL) == "(no registry digest)  id aaaaaaaaaaaa"


# ── (d) expected version ─────────────────────────────────────────────────────

def test_exact_expectation():
    e = dv.Expectation("exact", "v0.50.0-3-g3186c1b", "")
    assert e.matches("v0.50.0-3-g3186c1b")
    assert not e.matches("dev")
    assert not e.matches("v0.50.0")


@pytest.mark.parametrize("served, ok", [
    ("validation-3186c1b", True),        # CI's shallow clone: 7 characters
    ("validation-3186c1b5d", True),      # a bigger clone abbreviates longer
    ("validation-3186C1B", True),
    ("validation-3186c1c", False),       # another commit
    ("validation-3186c1", False),        # too short to trust
    ("validation-", False),
    ("validation-zzzzzzz", False),
    ("3186c1b", False),
    ("v0.50.0", False),
])
def test_commit_expectation_compares_ci_short_sha_as_a_prefix(served, ok):
    assert dv.Expectation("commit", SHA, "").matches(served) is ok


def test_changed_expectation():
    assert dv.Expectation("changed", "v0.49.0", "").matches("v0.50.0")
    assert not dv.Expectation("changed", "v0.49.0", "").matches("v0.49.0")
    assert dv.Expectation("changed", "", "").matches("anything")


def test_describe():
    assert dv.Expectation("commit", SHA, "").describe() == "validation-3186c1b"
    assert dv.Expectation("changed", "", "").describe() == "any version"
    assert dv.Expectation("changed", "v1.0.0", "").describe() == "anything but v1.0.0"


def test_latest_release_tag_compares_numerically_and_ignores_other_tags():
    tags = ["v0.9.0", "v0.10.0", "validation", "v0.10.0-rc1", "v0.9.12", "junk"]
    assert dv.latest_release_tag(tags) == "v0.10.0"
    assert dv.latest_release_tag(["validation"]) is None


def fake_git(answers):
    calls = []

    def git(args):
        calls.append(args)
        return answers.get(tuple(args))
    git.calls = calls
    return git


def test_local_build_expects_its_own_version():
    git = fake_git({})
    expectation, _ = dv.derive_expectation("validation", "v0.50.0-3-g3186c1b", git)
    assert expectation == dv.Expectation("exact", "v0.50.0-3-g3186c1b", "the version baked into this build")
    assert git.calls == []


def test_untagged_local_build_cannot_be_told_apart():
    expectation, why = dv.derive_expectation("validation", "dev", fake_git({}))
    assert expectation is None
    assert "'dev'" in why


def test_ci_validation_image_expects_the_validation_tag_commit():
    git = fake_git({("rev-parse", "validation^{commit}"): SHA + "\n"})
    expectation, _ = dv.derive_expectation("validation", None, git)
    assert expectation.kind == "commit" and expectation.value == SHA


def test_ci_validation_image_without_a_local_tag_is_unknown():
    expectation, why = dv.derive_expectation("validation", None, fake_git({}))
    assert expectation is None and "validation" in why


def test_prod_expects_the_newest_release_tag():
    git = fake_git({("tag", "--list", "v*"): "v0.9.0\nv0.10.0\nv0.10.0-rc1\n"})
    expectation, _ = dv.derive_expectation("prod", None, git)
    assert expectation == dv.Expectation("exact", "v0.10.0", "CI stamps :latest with the newest release tag")


def test_prod_without_release_tags_is_unknown():
    expectation, why = dv.derive_expectation("prod", None, fake_git({("tag", "--list", "v*"): ""}))
    assert expectation is None and "release tag" in why


# ── version polling ──────────────────────────────────────────────────────────

def poll(answers, expectation, timeout=120.0, interval=3.0):
    clock = FakeClock()
    answers = list(answers)
    fetches = []

    def fetch():
        fetches.append(clock.now)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    result = dv.wait_for_version(fetch, expectation, timeout=timeout, interval=interval,
                                 clock=clock, sleep=clock.sleep, log=lambda _: None)
    return result, fetches, clock


EXACT = dv.Expectation("exact", "v0.50.0", "")


def test_polls_through_errors_and_the_old_version_until_the_new_one_answers():
    result, fetches, _ = poll(
        [ConnectionRefusedError("refused"), "v0.49.0", "v0.49.0", "v0.50.0"], EXACT)
    assert result == dv.VersionResult(True, "v0.50.0", None)
    assert len(fetches) == 4


def test_timeout_reports_the_version_still_served():
    result, fetches, clock = poll(["v0.49.0"], EXACT, timeout=120, interval=3)
    assert result == dv.VersionResult(False, "v0.49.0", None)
    assert clock.now - 1000.0 <= 120
    assert len(fetches) == 41  # t=0,3,...,120


def test_timeout_with_no_answer_reports_the_error():
    result, _, _ = poll([OSError("502 Bad Gateway")], EXACT, timeout=10, interval=3)
    assert not result.ok
    assert result.error == "OSError: 502 Bad Gateway"


def test_error_after_a_wrong_version_keeps_both():
    result, _, _ = poll(["v0.49.0", ValueError("bad json")], EXACT, timeout=6, interval=3)
    assert result == dv.VersionResult(False, "v0.49.0", "ValueError: bad json")


def test_first_answer_matching_returns_without_sleeping():
    result, _, clock = poll(["v0.50.0"], EXACT)
    assert result.ok and clock.sleeps == []


def test_unexpected_exceptions_are_not_swallowed():
    with pytest.raises(KeyError):
        poll([KeyError("bug")], EXACT)


def test_fetch_version_reads_the_version_field(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return json.dumps({"version": "v0.50.0"}).encode()

    def urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Response()

    monkeypatch.setattr(dv.urllib.request, "urlopen", urlopen)
    assert dv.fetch_version("https://val.traxjourney.com/") == "v0.50.0"
    assert seen == {"url": "https://val.traxjourney.com/api/version", "timeout": 10.0}


def test_fetch_version_rejects_an_answer_without_a_version(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b'{"status": "ok"}'

    monkeypatch.setattr(dv.urllib.request, "urlopen", lambda request, timeout: Response())
    with pytest.raises(ValueError):
        dv.fetch_version("https://val.traxjourney.com")


# ── host snapshots ───────────────────────────────────────────────────────────

HOST = dv.Host("host.invalid", 2222, "deployer", "/keys/id", VAL_DIR)


def transcript(scenario, image=True, inspect=None):
    return "\n".join([
        "### ps", fixture(scenario, "compose_ps.ndjson"),
        "### services", fixture(scenario, "config_services.txt"),
        "### inspect", json.dumps(inspect) if inspect is not None else fixture(scenario, "docker_inspect.json"),
        "### image", fixture("image_inspect.json") if image else "[]",
    ])


def test_ssh_argv_and_remote_commands():
    assert HOST.ssh_argv("x") == ["ssh", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-i", "/keys/id", "-p", "2222",
                                  "deployer@host.invalid", "x"]
    assert dv.compose_images_command(HOST) == "cd /opt/traxjourney-val || exit 1; docker compose config --images"
    command = dv.host_state_command(HOST, VAL)
    assert command.startswith("cd /opt/traxjourney-val || exit 1; set -e; ")
    for part in ("docker compose ps --all --format json", "docker compose config --services",
                 "docker compose ps --all -q | xargs -r docker inspect", f"docker image inspect {VAL}"):
        assert part in command


def test_deploy_commands_pull_then_up_and_never_take_the_stack_down():
    """#439: `down` first left the environment stopped whenever the pull failed."""
    assert dv.pull_command(HOST) == "cd /opt/traxjourney-val || exit 1; docker compose pull 1>&2"
    assert dv.up_command(HOST) == "cd /opt/traxjourney-val || exit 1; docker compose up -d 1>&2"
    assert dv.baseline_command(HOST) == (
        "cd /opt/traxjourney-val || exit 1; docker compose ps --all -q | xargs -r docker inspect")
    source = Path(dv.__file__).read_text(encoding="utf-8")
    assert "compose down" not in source


def test_remote_directory_is_shell_quoted():
    host = dv.Host("h", 22, "u", "k", "/opt/my app")
    assert dv.compose_images_command(host).startswith("cd '/opt/my app' || exit 1; ")


def test_split_sections():
    assert dv.split_sections("noise\n### a\n1\n2\n### b\n") == {"a": "1\n2", "b": ""}


def test_read_host_healthy():
    report, pending = dv.read_host(HOST, VAL, lambda cmd: (0, transcript("healthy")))
    assert report.problems == [] and pending == []
    assert report.image.endswith("id cad9a2c87176")


def test_read_host_combines_service_and_image_problems():
    report, _ = dv.read_host(HOST, VAL, lambda cmd: (0, transcript("crashloop", image=False)))
    assert report.problems == [
        "traxjourney-val-worker-1 is crash-looping (Restarting (3) 3 seconds ago)",
        f"{VAL} is not present on the host after the pull",
    ]


def test_read_host_subtracts_the_baseline():
    _, _, inspect = snapshot("healthy")
    before = with_restarts(inspect, "traxjourney-val-redis-1", 4)
    report, _ = dv.read_host(HOST, VAL, lambda cmd: (0, transcript("healthy", inspect=before)),
                             baseline=dv.restart_baseline(before))
    assert report.problems == []


def test_read_host_ssh_failure_and_garbage():
    report, _ = dv.read_host(HOST, VAL, lambda cmd: (255, ""))
    assert report.problems == ["could not read container state from the host (ssh exit 255)"]
    report, _ = dv.read_host(HOST, VAL, lambda cmd: (0, "### ps\n{not json\n"))
    assert len(report.problems) == 1 and report.problems[0].startswith("could not parse")


def snapshots(*results):
    results = list(results)
    taken = []

    def take():
        taken.append(True)
        return results.pop(0) if len(results) > 1 else results[0]
    take.taken = taken
    return take


OK = (dv.HostReport([], ["a: Up"], "img"), [])
STARTING = (dv.HostReport([], ["redis: starting"], "img"), ["redis healthcheck is still starting"])


def test_host_is_not_read_before_the_settle_window():
    clock = FakeClock()
    take = snapshots(OK)
    report = dv.wait_for_host(take, started=clock.now - 4, settle=15, clock=clock, sleep=clock.sleep)
    assert report.problems == []
    assert clock.sleeps == [11]


def test_host_read_at_once_when_the_settle_window_has_passed():
    clock = FakeClock()
    dv.wait_for_host(snapshots(OK), started=clock.now - 100, settle=15, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


def test_starting_containers_are_re_read_until_settled():
    clock = FakeClock()
    take = snapshots(STARTING, STARTING, OK)
    report = dv.wait_for_host(take, started=clock.now, settle=0, timeout=60, interval=5,
                              clock=clock, sleep=clock.sleep)
    assert report.problems == []
    assert len(take.taken) == 3


def test_containers_still_starting_at_the_deadline_fail():
    clock = FakeClock()
    take = snapshots((dv.HostReport([], [], "img"), ["redis healthcheck is still starting"]))
    report = dv.wait_for_host(take, started=clock.now, settle=0, timeout=60, interval=5,
                              clock=clock, sleep=clock.sleep)
    assert report.problems == ["redis healthcheck is still starting, still not settled after 60s"]
    assert len(take.taken) == 13


def test_hard_failure_is_not_retried():
    clock = FakeClock()
    take = snapshots((dv.HostReport(["worker is crash-looping"], [], "img"), ["redis starting"]))
    report = dv.wait_for_host(take, started=clock.now, settle=0, clock=clock, sleep=clock.sleep)
    assert report.problems == ["worker is crash-looping"]
    assert len(take.taken) == 1


# ── one SSH session (#439) ───────────────────────────────────────────────────
# A passphrase-protected key without an agent asks once per connection, and
# Windows' OpenSSH has no ControlMaster to share one. So everything the deploy
# does on the host goes through a single `ssh ... sh` whose stdin carries the
# commands. These run the protocol against a real local `sh`.

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


def test_ssh_gives_up_on_a_silently_dropped_connection():
    """The one session idles through the version wait and the re-check. A
    connection dropped without a FIN (NAT timeout, host gone) would otherwise
    block readline() forever; keepalives turn it into ssh exit 255 within a
    minute (15 s x 4)."""
    argv = HOST.ssh_argv("sh")
    options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    assert "ServerAliveInterval=15" in options and "ServerAliveCountMax=4" in options
    assert argv.index("-o") < argv.index("deployer@host.invalid")


def test_open_session_runs_one_remote_shell():
    seen = {}

    class Popen:
        def __init__(self, argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs

    dv.open_session(HOST, popen=Popen)
    assert seen["argv"] == ["ssh", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-i", "/keys/id", "-p", "2222",
                           "deployer@host.invalid", "sh"]
    assert seen["kwargs"]["stdin"] == subprocess.PIPE and seen["kwargs"]["stdout"] == subprocess.PIPE
    assert not seen["kwargs"].get("text"), "text mode writes CRLF on Windows"


@needs_sh
def test_session_runs_every_command_in_the_same_shell():
    with dv.Session([SH]) as session:
        first = session.run("echo $$")
        assert session.run("echo one; echo two") == (0, "one\ntwo\n")
        assert session.run("echo $$") == first


@needs_sh
def test_session_reports_exit_codes_without_losing_the_shell():
    with dv.Session([SH]) as session:
        assert session.run("exit 3") == (3, "")
        assert session.run("false") == (1, "")
        assert session.run("cd /nonexistent-439 2>/dev/null || exit 1; echo no") == (1, "")
        assert session.run("echo still here") == (0, "still here\n")


@needs_sh
def test_session_keeps_output_exactly():
    with dv.Session([SH]) as session:
        assert session.run("printf 'no newline'") == (0, "no newline")
        assert session.run("printf 'a\\n\\n'") == (0, "a\n\n")
        assert session.run("printf ''") == (0, "")


def test_session_sends_lf_only():
    """A carriage return would reach the remote shell as part of the command.
    Checked on the bytes: Git for Windows' sh tolerates CRs, Debian's does not."""
    import io

    class Stdin(io.BytesIO):
        def close(self):
            pass

    class Proc:
        def __init__(self, argv, **kwargs):
            self.stdin = Stdin()
            self.stdout = self

        def readline(self):
            marker = re.search(rb"(__deploy_verify_[0-9a-f]+__)", self.stdin.getvalue()).group(1)
            return marker + b" 0\n"

        def wait(self, timeout=None):
            return 0

    session = dv.Session(["ssh"], popen=Proc)
    assert session.run("echo end") == (0, "")
    sent = session.proc.stdin.getvalue()
    assert b"echo end\n" in sent and b"\r" not in sent


@needs_sh
def test_commands_cannot_read_the_session_stream():
    """The commands arrive on the shell's stdin; one that reads stdin must not eat the next."""
    with dv.Session([SH]) as session:
        assert session.run("cat; echo after") == (0, "after\n")
        assert session.run("echo next") == (0, "next\n")


@needs_sh
def test_session_that_ended_reports_the_ssh_exit_code():
    """What a refused key looks like: ssh exits before running anything."""
    with dv.Session([SH, "-c", "exit 255"]) as session:
        code, out = session.run("echo never")
    assert code == 255 and out == ""


# ── the commands deploy.ps1 runs ─────────────────────────────────────────────

def args(*extra, command="deploy", target="validation"):
    return dv.parse_args([
        command, "--ssh-host", "host.invalid", "--ssh-port", "22", "--ssh-user", "deployer",
        "--ssh-key", "/keys/id", "--dir", VAL_DIR, "--image", VAL,
        "--url", "https://val.traxjourney.com", "--target", target, *extra,
    ])


SESSION_HOST = dv.Host("host.invalid", 22, "deployer", "/keys/id", VAL_DIR)


class FakeHost:
    """The far end of the deploy's SSH session: answers each command it is sent."""

    def __init__(self, images=f"{VAL}\nredis:7-alpine\n", baseline="[]", pull=0, up=0,
                 states=("healthy",), connect=0):
        self.images, self.baseline, self.pull, self.up = images, baseline, pull, up
        self.states = list(states)
        self.connect = connect
        self.commands = []
        self.opened = 0

    def session(self, host):
        assert host == SESSION_HOST
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command):
        if self.connect:
            return self.connect, ""
        kind = {
            dv.compose_images_command(SESSION_HOST): "images",
            dv.baseline_command(SESSION_HOST): "baseline",
            dv.pull_command(SESSION_HOST): "pull",
            dv.up_command(SESSION_HOST): "up",
            dv.host_state_command(SESSION_HOST, VAL): "state",
        }[command]
        self.commands.append(kind)
        if kind == "images":
            return 0, self.images
        if kind == "baseline":
            return 0, self.baseline
        if kind in ("pull", "up"):
            return getattr(self, kind), ""
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return 0, state if state.startswith("###") else transcript(state)


def deploy(host, *extra, served=("v0.49.0", "v0.50.0"), push_exit=0, target="validation", git=None):
    clock = FakeClock()
    served = list(served)
    pushed = []

    def push(ref):
        pushed.append((ref, list(host.commands)))
        return push_exit

    code = dv.cmd_deploy(args(*extra, target=target), session=host.session,
                         fetch=lambda url: served.pop(0) if len(served) > 1 else served[0],
                         git=git or fake_git({}), push=push, clock=clock, sleep=clock.sleep)
    return code, pushed, clock


BUILT = ("--built-version", "v0.50.0")


def test_deploy_checks_the_host_then_pulls_then_starts_then_verifies(capsys):
    host = FakeHost()
    code, _, _ = deploy(host, *BUILT)
    out = capsys.readouterr().out
    assert code == 0, out
    assert host.commands == ["images", "baseline", "pull", "up", "state", "state"]
    assert "FAIL" not in out


def test_the_whole_deploy_is_one_ssh_session():
    """Including the re-reads while a healthcheck starts, and the final re-check."""
    host = FakeHost(states=("starting", "starting", "healthy"))
    code, _, _ = deploy(host, *BUILT)
    assert code == 0
    assert host.opened == 1
    assert host.commands.count("state") == 4


def test_a_failed_pull_leaves_the_running_stack_untouched(capsys):
    host = FakeHost(pull=1)
    code, _, _ = deploy(host, *BUILT)
    out = capsys.readouterr().out
    assert code == dv.EXIT_UNTOUCHED
    assert host.commands == ["images", "baseline", "pull"]
    assert "FAIL: `docker compose pull` failed (exit 1)" in out
    assert "the running containers were not touched" in out


def test_a_failed_up_is_a_failed_deploy(capsys):
    host = FakeHost(up=1)
    code, _, _ = deploy(host, *BUILT)
    assert code == 1
    assert host.commands == ["images", "baseline", "pull", "up"]
    assert "FAIL: `docker compose up -d` failed (exit 1)" in capsys.readouterr().out


def test_deploy_reports_how_long_the_pull_took(capsys):
    """The pull used to happen with the stack down; this is the downtime #439 removes."""
    deploy(FakeHost(), *BUILT)
    assert re.search(r"pull took \d+s", capsys.readouterr().out)


def test_a_host_on_another_image_stops_before_anything_is_pushed_or_pulled(capsys):
    host = FakeHost(images=fixture("config_images_example.txt"))
    code, pushed, _ = deploy(host, *BUILT, "--push", VAL)
    out = capsys.readouterr().out
    assert code == dv.EXIT_UNTOUCHED
    assert host.commands == ["images"]
    assert pushed == []
    assert f"FAIL: {VAL_DIR}/docker-compose.yml does not use {VAL}" in out


def test_an_unreachable_host_stops_before_anything_is_pushed(capsys):
    host = FakeHost(connect=255)
    code, pushed, _ = deploy(host, *BUILT, "--push", VAL)
    assert code == dv.EXIT_UNTOUCHED
    assert pushed == []
    assert "could not read the compose file" in capsys.readouterr().out


def test_a_local_build_is_pushed_after_the_host_check_and_before_the_pull():
    """Nothing leaves the machine until the host is known to pull what is pushed."""
    host = FakeHost()
    code, pushed, _ = deploy(host, *BUILT, "--push", VAL, "--push", "ghcr.io/rui-nar/traxjourney:v0.50.0")
    assert code == 0
    assert [ref for ref, _ in pushed] == [VAL, "ghcr.io/rui-nar/traxjourney:v0.50.0"]
    assert all(before == ["images"] for _, before in pushed)


def test_a_failed_push_stops_before_the_pull(capsys):
    host = FakeHost()
    code, pushed, _ = deploy(host, *BUILT, "--push", VAL, push_exit=1)
    assert code == dv.EXIT_UNTOUCHED
    assert host.commands == ["images"]
    assert f"FAIL: docker push {VAL} failed (exit 1)" in capsys.readouterr().out


def test_deploy_passes_and_prints_the_summary(capsys):
    host = FakeHost()
    code, _, _ = deploy(host, served=["v0.49.0", "validation-3186c1b"],
                        git=fake_git({("rev-parse", "validation^{commit}"): SHA}))
    out = capsys.readouterr().out
    assert code == 0
    assert "expected validation-3186c1b" in out
    assert "before   v0.49.0" in out
    assert "served   validation-3186c1b  OK" in out
    assert "ghcr.io/rui-nar/traxjourney@sha256:cad9a2c8" in out
    assert "traxjourney-val-worker-1: Up 11 seconds" in out
    assert "FAIL" not in out


def test_deploy_fails_on_a_version_that_never_changes(capsys):
    code, _, _ = deploy(FakeHost(), *BUILT, "--version-timeout", "30", served=["v0.49.0"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL: https://val.traxjourney.com/api/version reports v0.49.0, not v0.50.0, after 30s" in out
    assert "served   v0.49.0  FAIL" in out


def test_deploy_fails_on_a_crash_loop_even_when_the_version_is_right(capsys):
    host = FakeHost(states=("flapping",))
    code, _, _ = deploy(host, *BUILT)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL: traxjourney-val-worker-1 has restarted 1 time(s) during the deploy" in out
    assert host.commands.count("state") == 1, "no re-check once a check has failed"


def test_deploy_ignores_restarts_from_before_it_started(capsys):
    _, _, inspect = snapshot("healthy")
    before = with_restarts(inspect, "traxjourney-val-redis-1", 2)
    host = FakeHost(baseline=json.dumps(before), states=(transcript("healthy", inspect=before),))
    code, _, _ = deploy(host, *BUILT)
    assert code == 0, capsys.readouterr().out


def test_deploy_warns_when_only_a_change_was_checked(capsys):
    code, _, _ = deploy(FakeHost(), served=["v0.49.0", "v0.50.0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "WARNING: cannot tell which version" in out
    assert "only a change of version was checked" in out


# ── (e) the late-crash re-check (#439) ───────────────────────────────────────

def test_a_crash_after_the_checks_passed_is_caught(capsys):
    host = FakeHost(states=("healthy", "flapping"))
    code, _, clock = deploy(host, *BUILT)
    out = capsys.readouterr().out
    assert code == 1
    assert host.commands[-2:] == ["state", "state"]
    assert clock.sleeps[-1] == dv.RECHECK_SECONDS
    assert (f"FAIL: traxjourney-val-worker-1 has restarted 1 time(s) during the deploy, so it is crashing "
            f"({dv.RECHECK_SECONDS:.0f}s after the other checks passed)") in out


def test_a_container_gone_after_the_checks_passed_is_caught(capsys):
    host = FakeHost(states=("healthy", "exited"))
    code, _, _ = deploy(host, *BUILT)
    assert code == 1
    assert "FAIL: traxjourney-val-worker-1 is not running" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["starting", "created"])
def test_a_container_starting_again_at_the_re_check_fails(capsys, state):
    """Back to `starting` (or `created`) means it went away and came back since
    the checks passed. That is not a pass, even with no restart counted."""
    if state == "starting":
        again = "starting"  # the fixture: redis's healthcheck is starting
    else:
        _, ps, _ = snapshot("healthy")
        ps = [dict(e, State="created", Status="Created") if e["Service"] == "worker" else e for e in ps]
        again = transcript("healthy").replace(fixture("healthy", "compose_ps.ndjson"),
                                              "\n".join(json.dumps(e) for e in ps))
        assert '"State": "created"' in again
    host = FakeHost(states=("healthy", again))
    code, _, _ = deploy(host, *BUILT)
    out = capsys.readouterr().out
    assert code == 1, out
    assert f"({dv.RECHECK_SECONDS:.0f}s after the other checks passed)" in out


def test_the_re_check_waits_before_reading_again():
    host = FakeHost()
    _, _, clock = deploy(host, *BUILT)
    assert dv.RECHECK_SECONDS >= 15
    assert clock.sleeps[-1] == dv.RECHECK_SECONDS
    assert host.commands[-2:] == ["state", "state"]


# ── the version the banner shows (#439) ──────────────────────────────────────

def expect(*extra, target, git):
    return dv.cmd_expect(dv.parse_args(["expect", "--target", target, *extra]), git=git)


def test_banner_version_for_a_local_build_is_the_built_version(capsys):
    assert expect("--built-version", "v0.50.0-3-g3186c1b-dirty", target="validation", git=fake_git({})) == 0
    assert capsys.readouterr().out == "v0.50.0-3-g3186c1b-dirty\n"


def test_banner_version_for_ci_validation_is_the_validation_commit(capsys):
    git = fake_git({("rev-parse", "validation^{commit}"): SHA})
    assert expect(target="validation", git=git) == 0
    assert capsys.readouterr().out == "validation-3186c1b\n"


def test_banner_version_for_prod_is_the_newest_release(capsys):
    git = fake_git({("tag", "--list", "v*"): "v0.9.0\nv0.10.0\n"})
    assert expect(target="prod", git=git) == 0
    assert capsys.readouterr().out == "v0.10.0\n"


def test_banner_version_when_unknown_says_why(capsys):
    assert expect(target="prod", git=fake_git({})) == 0
    out = capsys.readouterr().out
    assert out.startswith("unknown version (") and "release tag" in out
