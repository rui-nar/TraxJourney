#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Build, push and deploy TraxJourney to the validation or production
    environment, then check the deploy actually took effect.

.DESCRIPTION
    -Target Validation (default) -> DEPLOY_VAL_URL, DEPLOY_VAL_DIR:
      1. Builds the Flutter web app.
      2. Builds the Docker image locally (:validation + version tag if present).
      3. Over ONE SSH connection: checks the host's compose file uses the image
         being deployed, pushes the image to GHCR, runs docker compose pull
         then up -d on the host, and verifies the deploy (see VERIFICATION).

    -Target Prod -> DEPLOY_PROD_URL, DEPLOY_PROD_DIR:
      Skips the build entirely - prod runs whatever :latest CI has published for
      the current tagged release (see .github/workflows). Step 3 only, without
      the push. Refuses to start from a working tree with uncommitted changes.

    No "docker compose down": the pull runs while the old containers keep
    serving, so a failed pull leaves the environment as it was, and up -d
    recreates only the containers whose image or configuration changed.

    One SSH connection, so a passphrase-protected key without an agent asks
    once. Windows' OpenSSH has no ControlMaster, so scripts/deploy_verify.py
    keeps one remote shell open and sends every host command down it.

    By default (Validation only) the image is built from the CURRENT working tree
    (the fast path for validating local, possibly-uncommitted changes; the
    version then ends in -dirty, and the script warns). Pass
    -FromMain to instead build a pristine export of origin/main in a throwaway
    git worktree, so the image is exactly what's on main - never contaminated by
    local edits. -FromMain has no effect with -Target Prod, since that path never
    builds.

    CONFIGURATION
    Host details live in deploy.env next to this script (gitignored). Copy
    deploy.env.example to deploy.env and fill it in. The script stops before
    building or deploying anything if a value is missing.

    VERIFICATION
    "docker compose pull" pulls whatever image the HOST's compose file names,
    and a container that crash-loops still counts as started, so a clean exit
    from pull / up -d proves nothing (issue #423). The script fails, with
    a non-zero exit, unless:
      a. the host's compose file names the image being deployed (checked
         before anything is pushed or changed on the host);
      b. the app containers run the image ID the tag was just pulled as;
      c. every compose service is running, healthy where it has a healthcheck,
         and has not restarted since the deploy began;
      d. <url>/api/version reports the expected version within 120 s: the
         built version for a local build, validation-<sha of the validation
         tag> for CI's :validation, the newest vX.Y.Z tag for :latest;
      e. 30 s after all that passed, (b) and (c) still hold.
    The banner shows the version from (d) before anything starts.
    The checks live in scripts/deploy_verify.py, which needs Python 3 (the
    repo's .venv, or python on PATH).

    THE OTHER WAY TO CUT :validation
    A session with no local Docker (a web Claude Code session, say) can produce
    the same image without this script, by force-pushing the floating `validation`
    git tag:

        git tag -f validation <commit-or-branch>
        git push origin validation --force

    docker-build.yml then builds ghcr.io/rui-nar/traxjourney:validation on
    ubuntu-latest - the same tag this script pushes, so the val host pulls it
    either way and needs no reconfiguration. Deploy that image with -SkipBuild.
    Building on Linux also sidesteps the CRLF shebang failure of issue #190,
    which is inherent to building from a Windows working tree.

    -TagOnly is that flow's first half wrapped in this script: it force-pushes
    the `validation` tag onto the tip of origin/main and stops. Nothing is built
    and nothing is deployed - once the docker-build run is green, deploy with
    -SkipBuild.

.PREREQUISITES
    - deploy.env filled in (see CONFIGURATION).
    - Docker Desktop running locally and logged in to GHCR (Validation only,
      and not with -SkipBuild):
          echo $env:GHCR_TOKEN | docker login ghcr.io -u rui-nar --password-stdin
    - SSH key auth to DEPLOY_HOST via DEPLOY_SSH_KEY for DEPLOY_USER.
    - docker-compose.yml present in DEPLOY_VAL_DIR and DEPLOY_PROD_DIR on the host.
    - Docker on the host logged in to GHCR.
    - Python 3 locally, for the verification.

.PARAMETER MapboxToken
    Mapbox public token passed as a Dart define at build time (Validation only).
    Defaults to MAPBOX_TOKEN from deploy.env.

.PARAMETER FromMain
    Build a pristine export of origin/main (in a throwaway git worktree) instead
    of the local working tree, so the deployed image is exactly what's on main.
    Ignored with -Target Prod, and with -SkipBuild, since neither builds.

.PARAMETER SkipBuild
    Deploy whatever :validation already exists in GHCR instead of building it.
    This is the deploy half of the `validation` git tag flow above. Ignored with
    -Target Prod, which never builds anyway.

.PARAMETER TagOnly
    Force-push the floating `validation` git tag onto the most recent commit of
    main, then exit - no build, no deploy:

        git tag -f validation origin/main
        git push origin validation --force

    origin/main (freshly fetched) rather than the local branch, so the tag lands
    on what is really on main whatever you have checked out. GitHub Actions then
    publishes :validation from that commit; deploy it afterwards with
    -SkipBuild. Only valid with -Target Validation; -FromMain and -SkipBuild are
    ignored, since this path neither builds nor deploys.

.PARAMETER Target
    Validation (default) deploys the :validation image to the val environment.
    Prod deploys to the prod environment by pulling whatever :latest CI already
    published.

.EXAMPLE
    .\deploy.ps1

.EXAMPLE
    .\deploy.ps1 -FromMain

.EXAMPLE
    # after `git push origin validation --force` and a green docker-build run
    .\deploy.ps1 -SkipBuild

.EXAMPLE
    .\deploy.ps1 -Target Prod

.EXAMPLE
    # cut :validation from the tip of main on CI, then deploy it when it's green
    .\deploy.ps1 -TagOnly
    .\deploy.ps1 -SkipBuild
#>
param(
    [string]$MapboxToken,
    [switch]$FromMain,
    [switch]$SkipBuild,
    [switch]$TagOnly,
    [ValidateSet('Validation', 'Prod')]
    [string]$Target = 'Validation'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# The repository, not a host setting: docker-build.yml publishes from it.
$GITHUB_REPO = "rui-nar/TraxJourney"

function Step([string]$n, [string]$total, [string]$msg) {
    Write-Host ""
    Write-Host "[$n/$total] $msg" -ForegroundColor Cyan
}

function Die([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# Every path that skips the build (-Target Prod, or -SkipBuild) pulls an image
# the "Build and publish Docker image" workflow published - if that workflow is
# still running (or queued), the tag may be stale or only half-pushed. Returns
# non-completed runs, or $null if `gh` isn't available/authenticated (in which
# case the check is skipped, not treated as a failure).
function Get-ActiveImageBuildRuns {
    $json = gh run list --repo $GITHUB_REPO --workflow docker-build.yml --limit 5 --json status,displayTitle,url,createdAt 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        Write-Host "  (couldn't check GitHub Actions status - gh not installed/authenticated; skipping this check)" -ForegroundColor DarkYellow
        return $null
    }
    # The parentheses matter on Windows PowerShell 5.1 (deploy.bat): its
    # ConvertFrom-Json emits a JSON array as ONE object, so without them
    # Where-Object sees the whole list at once instead of each run.
    return @(($json | ConvertFrom-Json) | Where-Object { $_.status -ne 'completed' })
}

# Prod never builds; Validation builds unless told not to.
$Building = ($Target -eq 'Validation') -and (-not $SkipBuild)

# The `validation` tag is the val environment's rolling label; prod is cut from
# vN.N.N release tags by bump_version_and_release.ps1, so there is nothing here
# for -Target Prod to move.
if ($TagOnly -and $Target -ne 'Validation') {
    Die '-TagOnly is only valid with -Target Validation - it moves the floating `validation` tag, which prod never uses.'
}

# Uncommitted changes to tracked files: the rule `git describe --dirty` uses to
# mark a build of this tree -dirty. Untracked files don't count. Prod runs CI's
# image, not this tree, but this tree runs the deploy (this script, its checks,
# the tags they read), so prod starts only from a clean checkout (issue #439).
$DirtyFiles = @(git status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) { Die "git status failed." }
if ($Target -eq 'Prod' -and $DirtyFiles.Count -gt 0) {
    Write-Host ""
    foreach ($f in $DirtyFiles) { Write-Host "  $f" -ForegroundColor Yellow }
    Die "The working tree has uncommitted changes (listed above). Prod deploys only from a clean checkout: commit or stash them and retry. Nothing was done."
}

# Resolve the source tree to build from. With -FromMain we build a pristine
# export of origin/main in a throwaway git worktree, so the image is exactly
# what's on main - never contaminated by local edits or untracked files. Without
# it we build the current working tree (the fast path for validating local work).
#
# --force: `validation` is a floating tag. Without it, fetching a `validation`
# that was moved from anywhere else (a web session, another machine) is
# rejected as "would clobber existing tag", which fails the fetch - and a stale
# local tag would make verification expect the wrong commit.
git fetch origin --tags --force | Out-Null
if ($LASTEXITCODE -ne 0) { Die "git fetch failed." }

# -- -TagOnly ----------------------------------------------------------------
# Hand the build to GitHub Actions instead of Docker Desktop: move the floating
# `validation` tag to the tip of origin/main - the remote tip, just refreshed by
# the fetch above, so it is what's actually on main and not a local branch that
# may be behind or carry unpushed commits - and stop. docker-build.yml publishes
# :validation from it; deploy with -SkipBuild.
if ($TagOnly) {
    $mainSha = git rev-parse --short origin/main
    if ($LASTEXITCODE -ne 0) { Die "git rev-parse origin/main failed." }
    $mainSha = $mainSha.Trim()
    $mainSubject = git log -1 --format=%s origin/main
    if ($LASTEXITCODE -ne 0) { Die "git log on origin/main failed." }

    Write-Host ""
    Write-Host "  Moving tag 'validation' -> origin/main @ $mainSha  $mainSubject" -ForegroundColor Cyan
    git tag -f validation origin/main | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "git tag -f validation failed." }
    git push origin validation --force
    if ($LASTEXITCODE -ne 0) { Die "git push origin validation --force failed." }

    Write-Host ""
    Write-Host "===========================================" -ForegroundColor Green
    Write-Host "  Tagged validation at origin/main @ $mainSha" -ForegroundColor Green
    Write-Host "  CI  : https://github.com/$GITHUB_REPO/actions/workflows/docker-build.yml" -ForegroundColor Green
    Write-Host "  Then: .\deploy.ps1 -SkipBuild" -ForegroundColor Green
    Write-Host "===========================================" -ForegroundColor Green
    Write-Host ""
    exit 0
}

# -- Configuration -----------------------------------------------------------
# Host details are the operator's, not the repository's: they live in the
# gitignored deploy.env (template: deploy.env.example). Read into a table, not
# $env:, so a stray variable in the calling shell can neither leak in nor stand
# in for a value missing from the file.
$ConfigFile = Join-Path $PSScriptRoot 'deploy.env'
if (-not (Test-Path $ConfigFile)) {
    Die "deploy.env not found at '$ConfigFile'. Copy deploy.env.example to deploy.env and fill it in."
}
. (Join-Path $PSScriptRoot 'Load-DotEnv.ps1')
$Config = Read-DotEnv -Path $ConfigFile

$RequiredKeys = @(
    'DEPLOY_HOST', 'DEPLOY_SSH_PORT', 'DEPLOY_USER', 'DEPLOY_SSH_KEY',
    'DEPLOY_IMAGE', 'DEPLOY_VAL_DIR', 'DEPLOY_VAL_URL', 'DEPLOY_PROD_DIR', 'DEPLOY_PROD_URL'
)
$missing = @($RequiredKeys | Where-Object { -not $Config.Contains($_) })
if ($missing.Count -gt 0) {
    Die "deploy.env is missing: $($missing -join ', '). See deploy.env.example."
}

$DeployHost = $Config['DEPLOY_HOST']
$SshPort    = $Config['DEPLOY_SSH_PORT']
$DeployUser = $Config['DEPLOY_USER']
$SshKey     = $Config['DEPLOY_SSH_KEY']
$Image      = $Config['DEPLOY_IMAGE']

if ($SshKey.StartsWith('~')) { $SshKey = $HOME + $SshKey.Substring(1) }
if (-not (Test-Path $SshKey)) { Die "DEPLOY_SSH_KEY '$SshKey' does not exist." }
if ($Image -match ':[^/]*$' -or $Image.Contains('@')) {
    Die "DEPLOY_IMAGE is the repository without a tag, as in deploy.env.example; got '$Image'."
}
if ($Building -and -not $MapboxToken) {
    if (-not $Config.Contains('MAPBOX_TOKEN')) {
        Die "MAPBOX_TOKEN is missing from deploy.env; the web build needs it (or pass -MapboxToken)."
    }
    $MapboxToken = $Config['MAPBOX_TOKEN']
}

# Verification runs in Python: the repo's .venv if there is one, else python
# on PATH. The Windows Store "python" alias exists without Python installed,
# so the interpreter is proven by running it.
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }
$ErrorActionPreference = "Continue"
& $Python -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>$null | Out-Null
$pythonOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $pythonOk) { Die "Python 3.9+ is needed to verify the deploy; none found in .venv or on PATH." }
$Verifier = Join-Path $PSScriptRoot 'scripts\deploy_verify.py'

$Worktree = $null
if ($FromMain -and $Building) {
    git worktree prune | Out-Null   # drop stale registrations from any crashed run
    $Worktree = Join-Path ([System.IO.Path]::GetTempPath()) `
        ("traxjourney-main-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))
    Write-Host "  Exporting origin/main -> $Worktree" -ForegroundColor DarkGray
    git worktree add --detach $Worktree origin/main | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "git worktree add failed." }
    $SrcRoot    = $Worktree
    $VersionRef = "origin/main"
} else {
    $SrcRoot    = (Get-Location).Path
    $VersionRef = "HEAD"
}

# Resolve version tag - only set if the source ref is exactly on a tag.
$ErrorActionPreference = "Continue"
$Version = ""
$_tag = git describe --tags --exact-match $VersionRef 2>$null
if ($LASTEXITCODE -eq 0) { $Version = $_tag.Trim() }

$FullVersion = "dev"
if ($Worktree) {
    # --dirty cannot name a commit, and the throwaway worktree is clean anyway.
    $_full = git describe --tags --long origin/main 2>$null
} else {
    $_full = git describe --tags --long --dirty 2>$null
}
if ($LASTEXITCODE -eq 0) { $FullVersion = $_full.Trim() }
$ErrorActionPreference = "Stop"

# Val builds uncommitted work on purpose (the fast path above); the version
# says so. It must never be published under the release tag its commit has.
if ($Building -and -not $Worktree -and $DirtyFiles.Count -gt 0) {
    $Version = ""
    Write-Host ""
    Write-Host "WARNING: the working tree has uncommitted changes; building it as ${FullVersion}:" -ForegroundColor Yellow
    foreach ($f in $DirtyFiles) { Write-Host "  $f" -ForegroundColor Yellow }
}

if (-not $Building)  { $sourceLabel = "GHCR (no local build)" }
elseif ($FromMain)   { $sourceLabel = "origin/main (hermetic)" }
else                 { $sourceLabel = "local working tree" }

$targetBase = if ($Target -eq 'Prod') { $Config['DEPLOY_PROD_DIR'] } else { $Config['DEPLOY_VAL_DIR'] }
$targetUrl  = if ($Target -eq 'Prod') { $Config['DEPLOY_PROD_URL'] } else { $Config['DEPLOY_VAL_URL'] }
$targetTag  = if ($Target -eq 'Prod') { "latest" } else { "validation" }
$targetUrl  = $targetUrl.TrimEnd('/')

# What the server will report once deployed. For Prod and -SkipBuild that is
# CI's stamp on the image it pulls, not this checkout's HEAD (issue #439).
$ExpectArgs = @('--target', $Target.ToLowerInvariant(), '--repo', $PSScriptRoot)
if ($Building) { $ExpectArgs += @('--built-version', $FullVersion) }
$DeployVersion = & $Python $Verifier expect @ExpectArgs
if ($LASTEXITCODE -ne 0) { Die "Could not work out which version this deploy serves." }

Write-Host ""
Write-Host "--------------------------------------------" -ForegroundColor Green
Write-Host "  TraxJourney  -  deploy $DeployVersion ($Target)" -ForegroundColor Green
Write-Host "  source: $sourceLabel" -ForegroundColor Green
Write-Host "  -> ${Image}:${targetTag}" -ForegroundColor Green
Write-Host "  -> ${DeployHost}:${targetBase}  ($targetUrl)" -ForegroundColor Green
Write-Host "--------------------------------------------" -ForegroundColor Green

$DeployArgs = @(
    '--ssh-host', $DeployHost, '--ssh-port', $SshPort, '--ssh-user', $DeployUser, '--ssh-key', $SshKey,
    '--dir', $targetBase, '--image', "${Image}:${targetTag}", '--url', $targetUrl,
    '--target', $Target.ToLowerInvariant(), '--repo', $PSScriptRoot
)
if ($Building) { $DeployArgs += @('--built-version', $FullVersion) }

try {

if (-not $Building) {
    Write-Host ""
    Write-Host "  Checking for an in-progress image build on GitHub Actions..." -ForegroundColor Cyan
    $activeRuns = Get-ActiveImageBuildRuns
    # Not .Count: a function returning one run hands back the run itself, which
    # has no Count under StrictMode on Windows PowerShell 5.1.
    if ($activeRuns) {
        Write-Host ""
        foreach ($r in $activeRuns) {
            Write-Host "  - $($r.displayTitle) [$($r.status)] $($r.url)" -ForegroundColor Yellow
        }
        Die "A 'Build and publish Docker image' run is still in progress on GitHub Actions - you're deploying too soon. Deploying now would pull a stale or half-published :$targetTag. Wait for it to finish, then retry."
    }
}

if ($Building) {

# -- 1. Build Flutter web ------------------------------------------------------
Step 1 3 "Building Flutter web..."
Push-Location (Join-Path $SrcRoot 'flutter_client')
flutter build web --release `
  --dart-define=APP_VERSION=$FullVersion `
  --dart-define=MAPBOX_TOKEN=$MapboxToken
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Flutter build failed." }
Pop-Location
$webClient = Join-Path $SrcRoot 'web_client'
if (Test-Path $webClient) { Remove-Item -Recurse -Force $webClient }
Copy-Item -Recurse (Join-Path $SrcRoot 'flutter_client/build/web') $webClient
Write-Host "  Flutter web build ready in $webClient"

# -- 2. Build Docker image -----------------------------------------------------
Step 2 3 "Building Docker image..."

# :validation is the rolling dev label pushed by this script.
# :latest is reserved for clean version tags (set by GitHub Actions / CI).
$tags = @("-t", "${Image}:validation")
if ($Version) { $tags += @("-t", "${Image}:${Version}") }
# The server reports APP_VERSION from /api/version; without the build arg a
# local image says "dev" while its web client carries $FullVersion.
docker build @tags --build-arg "APP_VERSION=$FullVersion" $SrcRoot
if ($LASTEXITCODE -ne 0) { Die "Docker build failed." }

# Pushed by the deploy step, once the host is known to pull this image.
$DeployArgs += @('--push', "${Image}:validation")
if ($Version) { $DeployArgs += @('--push', "${Image}:${Version}") }

} else {
    Write-Host ""
    Write-Host "[1-2/3] Skipping build - pulling :$targetTag as already published to GHCR." -ForegroundColor Cyan
}

# -- 3. Deploy -----------------------------------------------------------------
# Both environments are plain Docker on the same Debian host, so one script
# serves both - only $targetBase differs. deploy_verify.py does every host step
# over one SSH connection, so a passphrase is asked once (issue #439): it
# checks the host's compose file, pushes a local build, runs docker compose
# pull then up -d (no down: a failed pull leaves the running containers alone),
# and verifies the result.
Step 3 3 "Deploying ($Target) on $DeployHost..."
& $Python $Verifier deploy @DeployArgs
if ($LASTEXITCODE -eq 3) { Die "Stopped before the running containers were touched (see FAIL lines above)." }
if ($LASTEXITCODE -ne 0) { Die "The deploy did not take effect as expected (see FAIL lines above). The containers were left as they are." }

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "  Deployed and verified ($Target)" -ForegroundColor Green
Write-Host "  App : $targetUrl" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""

}
finally {
    # Always tear down the throwaway worktree, even on failure. (PowerShell runs
    # finally on `exit` from within try; a stale registration is also swept by
    # `git worktree prune` at the next -FromMain run.)
    if ($Worktree -and (Test-Path $Worktree)) {
        Write-Host "  Cleaning up worktree $Worktree" -ForegroundColor DarkGray
        git worktree remove --force $Worktree 2>$null | Out-Null
        git worktree prune 2>$null | Out-Null
    }
}
