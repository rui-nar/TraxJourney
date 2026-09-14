"""deploy.ps1 invariants.

The -TagOnly path hands the :validation build to GitHub Actions by force-pushing
the floating `validation` tag. Two things make it dangerous if they ever drift:
it must tag the freshly fetched `origin/main` (a local `main` may be behind, or
carry unpushed commits, and would publish an image nobody else can reproduce),
and it must stop before the build/deploy steps, since a force-push that then
went on to build and deploy locally is not what the flag promises.

Since issue #423 the script is tracked, and its host details live in the
gitignored deploy.env. The rest of this file keeps it that way: no host, user,
key, directory, URL, image or token written into the script, every key it reads
documented in deploy.env.example, and a missing setting stopping the script
before it builds or deploys anything.

Most assertions are on the script text, because that is where such a mistake
would live, and no test host has the GHCR/VPS credentials to run it. The
behavioural ones run the script under every installed PowerShell edition, with a
config that stops it before any network access.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy.ps1"
EXAMPLE = ROOT / "deploy.env.example"
LOADER = ROOT / "Load-DotEnv.ps1"

TEXT = SCRIPT.read_text(encoding="utf-8-sig")
LINES = TEXT.splitlines()


def _code_lines():
    """Script lines with comments and the comment-based help block dropped."""
    out, in_help = [], False
    for line in LINES:
        stripped = line.strip()
        if stripped.startswith("<#"):
            in_help = True
        if in_help:
            if stripped.endswith("#>"):
                in_help = False
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def _index_of(pattern):
    """Index of the first code line matching `pattern`, or -1."""
    for i, line in enumerate(_code_lines()):
        if re.search(pattern, line):
            return i
    return -1


def test_tag_only_is_declared_as_a_switch():
    assert re.search(r"^\s*\[switch\]\$TagOnly,?\s*$", TEXT, re.MULTILINE), (
        "expected a [switch]$TagOnly parameter in the param block"
    )


def test_tag_only_is_rejected_outside_validation():
    """The user asked for a flag that is only valid with -Target Validation."""
    guard = _index_of(r"if \(\$TagOnly -and \$Target -ne 'Validation'\)")
    assert guard != -1, "expected -TagOnly to be rejected unless -Target is Validation"

    action = _index_of(r"git tag -f validation")
    assert action != -1, "expected -TagOnly to move the validation tag"
    assert guard < action, "the -Target guard must run before the tag is moved"


def test_tag_only_runs_the_two_documented_git_commands():
    """The flag is exactly the manual flow: tag origin/main, force-push the tag."""
    code = "\n".join(_code_lines())

    fetch = _index_of(r"git fetch origin")
    tag = _index_of(r"^\s*git tag -f validation origin/main\b")
    assert tag != -1, "expected `git tag -f validation origin/main`"
    assert fetch != -1 and fetch < tag, (
        "origin/main is only as fresh as the last fetch — fetch before tagging it"
    )

    push = _index_of(r"^\s*git push origin validation --force\b")
    assert push != -1, "expected `git push origin validation --force`"
    assert tag < push, "the tag must be moved before it is pushed"

    assert not re.search(r"git (tag|push)[^\n]*--delete", code), (
        "-TagOnly must only move the tag, never delete it"
    )


def test_tag_only_stops_before_building_or_deploying():
    code = _code_lines()
    block = _index_of(r"^if \(\$TagOnly\)")
    assert block != -1, "expected a top-level `if ($TagOnly)` block"

    exits = [i for i, line in enumerate(code) if re.search(r"^\s+exit 0\s*$", line)]
    assert exits, "expected -TagOnly to exit after pushing the tag"
    stop = exits[0]
    assert stop > block

    for pattern, what in (
        (r"flutter build web", "the Flutter build"),
        (r"docker build ", "the Docker build"),
        (r"docker push ", "the image push"),
        (r"\| ssh ", "the remote deployment"),
    ):
        step = _index_of(pattern)
        assert step != -1, f"{what} disappeared from the script"
        assert stop < step, f"-TagOnly must exit before {what}"


# -- configuration (issue #423) -----------------------------------------------

def _example_keys():
    keys = {}
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2)
    return keys


def _keys_read():
    return set(re.findall(r"\$Config\[\s*'([A-Z0-9_]+)'\s*\]", TEXT)) | set(
        re.findall(r"\$Config\.Contains\(\s*'([A-Z0-9_]+)'\s*\)", TEXT))


def _required_keys():
    block = re.search(r"\$RequiredKeys\s*=\s*@\((.*?)\)", TEXT, re.DOTALL)
    assert block, "expected a $RequiredKeys = @(...) list"
    return set(re.findall(r"'([A-Z0-9_]+)'", block.group(1)))


def test_example_lists_exactly_the_keys_the_script_reads():
    read = _keys_read()
    assert read, "expected the script to read settings as $Config['KEY']"
    assert set(_example_keys()) == read


def test_every_host_setting_is_required():
    """Only MAPBOX_TOKEN is optional: -MapboxToken can stand in, and only builds need it."""
    assert _required_keys() == _keys_read() - {"MAPBOX_TOKEN"}


def test_example_carries_no_host_user_or_token():
    keys = _example_keys()
    for key in ("DEPLOY_HOST", "DEPLOY_USER", "MAPBOX_TOKEN"):
        assert keys[key] == "", f"{key} must be left blank in the tracked example"
    assert keys["DEPLOY_IMAGE"] == "ghcr.io/rui-nar/traxjourney"


@pytest.mark.parametrize("pattern, what", [
    (r"\b\d{1,3}(\.\d{1,3}){3}\b", "an IP address"),
    (r"\bpk\.[A-Za-z0-9_-]{10,}", "a Mapbox token"),
    (r"\.ssh[\\/]", "an SSH key path"),
    (r"/opt/", "a host directory"),
    (r"traxjourney\.com", "an environment URL"),
    (r"ghcr\.io", "the image"),
    (r"(?i)\b(VPS|DEPLOY)_[A-Z_]*\s*=\s*[\"']", "a setting assigned a literal"),
])
def test_script_hard_codes_no_host_details(pattern, what):
    """The help text may describe the published setup; the code must read deploy.env."""
    code = "\n".join(_code_lines())
    assert not re.search(pattern, code), f"deploy.ps1 hard-codes {what}; put it in deploy.env"


@pytest.mark.parametrize("variable", ["DeployHost", "SshPort", "DeployUser", "SshKey", "Image"])
def test_connection_settings_come_from_the_config(variable):
    assignments = re.findall(rf"^\s*\${variable}\s*=\s*(.+)$", TEXT, re.MULTILINE)
    assert assignments, f"expected ${variable} to be assigned"
    assert assignments[0].strip().startswith("$Config['"), (
        f"${variable} must be read from deploy.env, not {assignments[0].strip()}"
    )


def test_script_is_ascii():
    """Windows PowerShell 5.1 (what deploy.bat runs) reads a BOM-less script as
    ANSI, where the bytes of a UTF-8 dash include a curly quote PowerShell
    treats as a string delimiter."""
    bad = [n for n, line in enumerate(SCRIPT.read_bytes().splitlines(), 1) if any(b > 127 for b in line)]
    assert not bad, f"non-ASCII bytes on lines {bad}"


def test_config_is_gitignored_and_the_script_is_not():
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "deploy.env", "deploy.env.example", "deploy.ps1"],
            cwd=ROOT, capture_output=True, text=True,
        )
    except OSError:
        pytest.skip("needs git")
    if ignored.returncode not in (0, 1):
        pytest.skip("needs a git checkout")
    assert ignored.stdout.split() == ["deploy.env"]


# -- versions and the remote step -----------------------------------------------

def test_local_build_stamps_the_server_with_the_client_version():
    """Without the build arg the server reports "dev" while the web client
    carries $FullVersion, and the deploy cannot be checked by version."""
    docker_build = _index_of(r"^\s*docker build ")
    assert docker_build != -1
    line = _code_lines()[docker_build]
    assert re.search(r'--build-arg\s+"APP_VERSION=\$FullVersion"', line), line
    assert re.search(r"--dart-define=APP_VERSION=\$FullVersion", TEXT)


def test_tags_are_force_fetched():
    """`validation` floats: a plain `git fetch --tags` rejects a moved tag and fails."""
    fetch = _index_of(r"^git fetch origin")
    assert fetch != -1
    line = _code_lines()[fetch]
    assert re.search(r"--tags\b", line) and re.search(r"--force\b", line), line


def test_remote_script_strips_carriage_returns():
    """This file is checked out CRLF on Windows and PowerShell pipes a trailing
    CRLF; bash would read `set -euo pipefail` with a carriage return on it."""
    ssh = _index_of(r"\| ssh ")
    assert ssh != -1
    assert _code_lines()[ssh].rstrip().endswith('"tr -d \'\\r\' | bash -s"')


# -- running it -----------------------------------------------------------------

POWERSHELLS = [exe for exe in ("pwsh", "powershell") if shutil.which(exe)]
needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")


@pytest.mark.skipif(not POWERSHELLS, reason="no PowerShell installed")
@pytest.mark.parametrize("script", [SCRIPT, LOADER], ids=lambda p: p.name)
@pytest.mark.parametrize("exe", POWERSHELLS or ["none"])
def test_script_parses(exe, script):
    """A syntax error here is only discovered at deploy time otherwise. Both
    editions where present: deploy.bat runs Windows PowerShell, a terminal
    usually pwsh."""
    check = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script.as_posix()}', "
        "[ref]$null, [ref]$errors) > $null; "
        "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script.name} does not parse:\n{result.stdout}{result.stderr}"


def _pwsh(command):
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@needs_pwsh
def test_read_dotenv_returns_a_table_without_touching_the_environment(tmp_path):
    env = tmp_path / "x.env"
    env.write_text(
        "# comment\n\nDEPLOY_HOST=example.invalid\nQUOTED=\"a b\"\nEQ=k=v\nEMPTY=\n=nokey\n"
        "PROBE_423_KEY='single'\n",
        encoding="utf-8",
    )
    out = _pwsh(
        f". '{LOADER.as_posix()}'; "
        f"$v = Read-DotEnv -Path '{env.as_posix()}'; "
        "($v.Keys -join ',') + '|' + $v['QUOTED'] + '|' + $v['EQ'] + '|' + $v['PROBE_423_KEY'] + '|' + "
        "[string]($null -eq $env:PROBE_423_KEY)"
    )
    assert out == "DEPLOY_HOST,QUOTED,EQ,PROBE_423_KEY|a b|k=v|single|True"


@needs_pwsh
def test_import_dotenv_still_sets_the_environment(tmp_path):
    """dev.ps1 and dev-server.ps1 rely on it, empty values included."""
    env = tmp_path / ".env"
    env.write_text("PROBE_423_KEY=\"from file\"\nPROBE_423_EMPTY=\n", encoding="utf-8")
    out = _pwsh(
        f". '{LOADER.as_posix()}'; $env:PROBE_423_EMPTY = 'kept'; "
        f"$ok = Import-DotEnv -Path '{env.as_posix()}'; "
        "\"$ok|$env:PROBE_423_KEY|$env:PROBE_423_EMPTY\""
    )
    assert out == "True|from file|kept"


@pytest.fixture
def sandbox(tmp_path):
    """A checkout holding only the script, whose origin is a local bare repo, so
    `git fetch origin` succeeds offline. Every config used with it fails before
    the script could reach a host."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(origin)], check=True)
    shutil.copy(SCRIPT, work / "deploy.ps1")
    shutil.copy(LOADER, work / "Load-DotEnv.ps1")
    return work


def _run_deploy(work, exe):
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(work / "deploy.ps1"), "-SkipBuild"],
        cwd=work, capture_output=True, text=True, timeout=120,
    )


every_powershell = pytest.mark.parametrize("exe", POWERSHELLS or [pytest.param("pwsh", marks=pytest.mark.skip("no PowerShell installed"))])


@every_powershell
def test_missing_config_file_stops_the_script(sandbox, exe):
    result = _run_deploy(sandbox, exe)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "deploy.env not found" in result.stdout
    assert "Copy deploy.env.example to deploy.env" in result.stdout


@every_powershell
def test_missing_keys_are_named(sandbox, exe):
    (sandbox / "deploy.env").write_text("DEPLOY_HOST=host.invalid\nDEPLOY_USER=\n", encoding="utf-8")
    result = _run_deploy(sandbox, exe)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ("deploy.env is missing: DEPLOY_SSH_PORT, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_IMAGE, "
            "DEPLOY_VAL_DIR, DEPLOY_VAL_URL, DEPLOY_PROD_DIR, DEPLOY_PROD_URL") in "".join(result.stdout.splitlines())

