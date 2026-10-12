# Move to uv — Plan for #603

## Problem

Python dependencies are a single unlocked `requirements.txt` installed with
`pip` in the Dockerfile, in three CI jobs and on the dev machine (venv created
by hand, Python by pyenv / `py`). Three things are wrong with it:

- **Nothing is locked.** Every Docker build resolves the newest versions the
  `>=` ranges allow. The SQLModel/SQLAlchemy pins in `requirements.txt` exist
  because exactly this put an untested SQLModel into production and caused 500s.
  The image a tag produces depends on the day it was built.
- **Test tooling ships in the image.** `pytest`, `pytest-cov`, `fakeredis` and
  `PyYAML` are installed in production because there is one list.
- **Each environment is set up a different way.** CI uses `setup-python` + pip,
  `rail-extract.yml` installs three different hand-picked sets, and local dev
  depends on a venv created by hand that the rename runbook had to recreate.

The owner asked (issue #603) for `uv sync` in the Dockerfile and uv everywhere
else in place of pyenv / virtualenv / pip.

## Current state

- `requirements.txt` — 40 packages, mostly `>=` floors; `sqlmodel==0.0.37`,
  `sqlalchemy==2.0.48` pinned; `urllib3>=2.6,<3`; `osmium>=4.0.0,<5.0.0`.
  Comments in it explain each non-obvious entry and must survive the move.
- `Dockerfile:33-34` — `COPY requirements.txt .` then
  `pip install --no-cache-dir -r requirements.txt` on `python:3.14-slim`.
  `entrypoint.sh` / `worker-entrypoint.sh` call `alembic`, `gunicorn`, `rq`,
  `python` from `PATH`. The ops docs run scripts in the image with
  `docker compose run --rm --entrypoint python traxjourney scripts/…`.
- `.dockerignore` already excludes `.venv/`.
- `.github/workflows/test.yml` — `setup-python` 3.14, `pip install -r
  requirements.txt`, `alembic upgrade head && alembic check`, then `pytest`.
- `.github/workflows/rail-extract.yml` — `plan` job: `pip install PyYAML`;
  `build` job: `pip install osmium requests PyYAML`; `publish` job:
  `pip install -r requirements.txt` (the corpus gate imports the server's
  resolver). `tests/test_rail_extract_workflow.py:626-629` asserts the last
  string; `:101` proves the plan command runs with osmium/requests unimportable.
- `.github/workflows/docker-build.yml` builds the image on tags only. **No CI
  job builds the image on a PR**, so a broken Dockerfile is first seen at
  release time.
- No `.github/dependabot.yml`.
- `tests/test_json_guard.py:112` reads the FastAPI floor out of
  `requirements.txt`.
- Local: `dev-server.ps1`, `dev.ps1` and `launch flutter only.bat` activate
  `.venv`; `deploy.ps1:288` runs the verifier with `.venv\Scripts\python.exe`
  (stdlib only) or `python` on PATH; `bump_version_and_release.ps1:108` runs
  `scripts/release_notes.py` (stdlib, lazy `anthropic` for `-Polish`) with
  `python` on PATH. `.claude/settings.json` allows `.venv/Scripts/pip install`.
- Docs naming pip/venv: `README.md:58-62`, `PROJECT_STRUCTURE.md:98`,
  `docs/RELEASING.md:164-174`, `docs/DEPLOYMENT_VPS.md:563`; hints in
  `scripts/release_notes.py:545`, `scripts/make_video_icons.py:12-16`,
  `bump_version_and_release.ps1:18-19`.
- Runtime-import audit of the packages proposed as dev-only (grep of
  non-test `.py` files): `pytest`, `pytest-cov`, `fakeredis` — none.
  `PyYAML` — only `scripts/build_rail_extract.py` and `scripts/route_corpus.py`,
  neither of which is in the image (`.dockerignore` keeps only the listed
  scripts) nor imported by one that is. `httpx` **is** runtime
  (`api/translations.py`) and stays. `osmium` and `requests` are runtime
  (`src/rail/builder.py` via `scripts/fetch_rail_data.py`, which runs in the
  image).
- Dev machine has uv 0.13.0 (the pins use 0.12.19, see Decision 4). Smart App Control blocks unsigned uv-tool shims
  (memory: os error 4551); the full suite is run in the py314 citest container.

## Decisions

1. **`pyproject.toml` + committed `uv.lock` replace `requirements.txt`.**
   `[tool.uv] package = false`: the repo is an application run from its root,
   not an installable package; building it would need a build backend and a
   layout change for nothing. `requires-python = ">=3.14"` and a
   `.python-version` of `3.14`. Rules out keeping `requirements.txt` generated
   by `uv export` (two sources of truth) and `uv pip install -r` (pip with a
   faster engine, still unlocked).
2. **Dependencies are split (owner decision 2026-10-12).**
   - `[project.dependencies]` — what the server and the in-image scripts
     import: everything in `requirements.txt` except the four below, `httpx`
     and `osmium`/`requests` included.
   - `[dependency-groups] rail` — `osmium`, `requests`, `PyYAML`: what
     `rail-extract.yml`'s `plan` and `build` jobs need without the server's
     dependencies. The specifiers for `osmium`/`requests` repeat the runtime
     ones exactly.
   - `dev` — `pytest`, `pytest-cov`, `fakeredis`, plus `{include-group =
     "rail"}` (the suite imports `build_rail_extract` and parses YAML).
   - `release` — `anthropic`, for `release_notes.py --polish`. It is never in
     the image.
   - `[tool.uv] default-groups = ["dev", "release"]`, so a plain `uv sync`
     gives a developer everything; the image and the rail jobs opt out
     explicitly with `--no-default-groups`.
   Rules out a flat list (keeps pytest in production) and `--with anthropic`
   at run time (unlocked).
3. **The image is built with `uv sync --locked --no-default-groups`** from a
   uv binary copied from the pinned `ghcr.io/astral-sh/uv:<version>` image,
   into `/app/.venv`, using the base image's Python
   (`UV_PYTHON_DOWNLOADS=never`), bytecode compiled
   (`UV_COMPILE_BYTECODE=1`), no cache left in the layer, and
   `ENV PATH="/app/.venv/bin:$PATH"` so `entrypoint.sh`, `worker-entrypoint.sh`
   and `docker compose run --entrypoint python` keep working unchanged.
   `pyproject.toml` and `uv.lock` are copied before `COPY . .` so the
   dependency layer is cached across code-only changes. `--locked` fails the
   build if the lock does not match `pyproject.toml`, instead of resolving
   afresh. Rules out installing into the system interpreter
   (`UV_PROJECT_ENVIRONMENT=/usr/local`): it works, but mixes our packages with
   the base image's and makes "what is in the image" harder to state.
4. **Builds pin uv exactly; everything else gets a minimum** (R1-6, R2-1,
   R2-2).
   - **Exact pins** where builds must be reproducible:
     - The Dockerfile pulls uv through a named stage,
       `FROM ghcr.io/astral-sh/uv:X.Y.Z AS uv`, then `COPY --from=uv /uv
       /bin/uv`. Dependabot's `docker` ecosystem updates `FROM` lines, which is
       how a new uv is proposed.
     - Every `astral-sh/setup-uv` step sets `version: "X.Y.Z"`.
   - **A floor, not a pin:** `[tool.uv] required-version = ">=X.Y.Z"` in
     `pyproject.toml`. An exact `==` here would also bind uv runs the repo does
     not control. Dependabot's own uv would refuse to lock (R2-1), and the
     owner's local uv would stop every `uv run` after each bump (R2-2). A
     floor only rejects a uv too old for the lock.
   - **Tests tie the builds together, not the floor** (R3-1):
     - The Dockerfile tag equals every setup-uv `version:` exactly. So the
       Dependabot PR that bumps the uv stage fails CI until the same PR moves
       the CI versions. That failure is how a uv update reaches the owner,
       and the only manual step.
     - The floor is only checked to be no newer than that tag. If the floor
       followed every bump, Dependabot's own uv, which trails upstream, would
       be refused again after each one (R2-1 reopened). Your local uv would
       need updating each time too.
   - The floor stays at the version chosen at delivery. It moves only on
     purpose, for instance when a newer lock format requires it. Then an
     older local uv reports that it is too old, and the recovery is
     `uv self update`, which the README states (U4).
   - The version is the newest stable at delivery that is at least two
     weeks old: 0.12.19 (released 2026-09-25). The dev machine runs 0.13.0,
     which is above the floor. 0.13.0 itself was only three days old at
     delivery, so it was corrected in fix unit F1.
   - **Action refs follow the repo's convention**, major tags, moved by
     Dependabot's `github-actions` updates (owner decision, round 2). The
     exception is `astral-sh/setup-uv`, which publishes no major tags after
     v7. It is pinned to the exact tag `@v10.2.0` (owner decision during
     delivery, 2026-10-12), which Dependabot also moves. "Exact pin" in the Review envelope means the
     uv binary's version, not the action ref.
5. **CI uses `astral-sh/setup-uv` and `uv sync --locked`**, then `uv run` for
   `alembic` and `pytest`. `setup-python` goes away; uv installs 3.14 from
   `.python-version`. The rail jobs: `plan` and `build` —
   `uv sync --locked --no-default-groups --only-group rail`; `publish` —
   `uv sync --locked --no-default-groups --group rail` (server deps for the
   corpus gate's resolver, plus PyYAML for `route_corpus.py`). The plan job's
   existing test still proves its command does not need osmium/requests.
   After that explicit sync, nothing in CI may sync again (R1-5): `uv run`
   otherwise re-syncs to the default groups before running, which would
   reinstall everything on every rail matrix leg. Both workflows set
   `UV_NO_SYNC: "1"` in their workflow-level `env:`, and a test asserts it.
6. **Two image checks in CI.**
   - **Runtime-only import check, every PR** (R1-1, owner override). In the
     `test` job, after the normal sync:
     `UV_PROJECT_ENVIRONMENT=$RUNNER_TEMP/runtime uv sync --locked
     --no-default-groups`, then that environment's `python` imports
     `api.router` and `src.jobs.worker` and runs
     `scripts/fetch_rail_data.py --help`. This catches server code that imports
     a package only the dev or release group provides, which the suite cannot
     see because it runs with every group installed. It takes seconds and
     needs no docker build.
   - **Image build smoke, path-filtered** (`image` job in `test.yml`):
     `docker build`, then inside the container import `api.router` and
     `src.jobs.worker` (with the env a boot needs), `import osmium`, assert
     `pytest` is **not** importable, and run `alembic --help`. It runs when the
     PR touches `Dockerfile`, `pyproject.toml`, `uv.lock`, `.dockerignore`,
     `entrypoint.sh` or `worker-entrypoint.sh`, and always on push to main.
     Without it, a Dependabot lock bump that breaks the image is only found at
     the next release. It pushes nothing and does not build the Flutter client
     (`web_client/` absent is fine for an import check).
7. **Dependabot keeps the lock and uv current** (owner decision):
   `.github/dependabot.yml` with three ecosystems:
   - `uv`: weekly; minor+patch grouped into one PR, majors one PR each.
   - `docker` for `/`: weekly, so the uv stage tag moves (Decision 4). It
     ignores `semver-minor` and `semver-major` updates of `python` (R2-3). The
     base is `python:3.14-slim` with no digest, so Dependabot would only ever
     propose 3.15, which the version-alignment test rejects. A CPython minor
     is a deliberate change of `.python-version`, the Dockerfile and
     `requires-python` together, not a Dependabot PR. The `3.14-slim` tag
     already floats to the latest 3.14 patch at each build.
   - `github-actions`: weekly, grouped.

   Merging stays manual. Security advisories still open their own PRs
   regardless of schedule.
8. **Local dev keeps `.venv` at the repo root**, which is where `uv sync` puts
   it, so the scripts that call `.venv\Scripts\python.exe` keep working; only
   their "no venv" messages change to say `uv sync`.
   **Locally, tools run as `uv run python -m <tool>`, never through their
   `.venv\Scripts\<tool>.exe` launcher** (R1-2). Tested on the dev machine on
   2026-10-12:
   - `uv venv --python 3.14` over the installed CPython copies a signed
     `python.exe`, which runs.
   - The console-script launchers uv writes (`pytest.exe`, `uvicorn.exe`, …)
     are NotSigned, and Smart App Control blocks them ("An Application Control
     policy has blocked this file").
   - A Python that uv downloads itself would not be signed either.

   So:
   - `dev-server.ps1` and `dev.ps1` run `uv run python -m uvicorn …` instead
     of activating the venv. `uv run` syncs a stale venv first.
   - `.claude/settings.json` allows `uv run python -m pytest`.
   - `bump_version_and_release.ps1` runs `uv run python
     scripts/release_notes.py`. The `release` group is a default group, so
     `-Polish` works after `uv sync`.
   - `deploy.ps1` keeps its `.venv`-or-PATH lookup and only its message
     changes. The verifier is stdlib-only, and `uv run` would add a sync to
     every deploy.
   - The README says to install CPython 3.14 (python.org or the Python install
     manager) and uv, and to run `uv sync`. uv prefers an installed matching
     interpreter, so it does not download one. It also says to use `python -m`
     for tools on Windows with Smart App Control.

   pyenv is not referenced anywhere in the repo.
   **graphify moves out of the project venv** (R1-4). `uv sync` is exact, so it
   uninstalls the pip-installed `graphifyy` from `.venv`. Before the first
   `uv sync` on the dev machine, graphify is installed into its own
   `py -3.14 -m venv %LOCALAPPDATA%\graphify\venv` with pip, and that
   `Scripts` directory goes on the user PATH. pip's launcher is unsigned but
   runs under SAC today: the current `.venv\Scripts\graphify.exe` does.
   `graphify update .` keeps its name. This is a dev-machine step, not repo
   code (see Owner actions).
9. **Historical documents are not rewritten**: `docs/*_PLAN.md`,
   `docs/reviews/`, `docs/repro/keepalive_502/`, and
   `docs/RENAME_TRAXJOURNEY_RUNBOOK.md` (a completed one-off runbook) describe
   what was true when they were written. `tools/flutter-test/` has no Python.

## Review envelope

REVIEW.md defaults apply, with these additions:

- **Supply chain.** New trusted inputs: the `ghcr.io/astral-sh/uv` image and
  the `astral-sh/setup-uv` action. The uv binary they provide is pinned to an
  exact version in every build (Decision 4). The action ref itself follows
  the repo's major-tag convention (owner decision, round 2), except setup-uv:
  it has no major tags after v7, so it is pinned to an exact release tag
  (Decision 4). The lock file
  carries hashes; `--locked` builds install exactly what it lists. A
  compromised upstream release is out of envelope (as with pip today); a build
  that installs an unpinned or floating uv binary is a finding.
- **Build reproducibility is in scope.** Two builds of one commit must install
  the same Python packages. Anything that resolves at build time instead of
  reading the lock is a finding. The base image (`python:3.14-slim`, its
  CPython patch and apt packages) floats on purpose, as today, so its security
  patches arrive with each build. An unpinned base digest is not a finding
  (owner decision, round 3).
- **The image's runtime contract is in scope:** `entrypoint.sh`,
  `worker-entrypoint.sh`, and every `docker compose run --entrypoint python
  … scripts/<x>.py` in `docs/DEPLOYMENT_VPS.md` must work unchanged on the
  new image.

## Boundaries crossed

- **API, schema, stored data, export formats: none.** No application code
  changes.
- **The image's contents change**: pytest, pytest-cov, fakeredis and PyYAML
  are no longer in it, and transitive versions become the locked ones rather
  than the newest. Nothing in the image imports the removed four (audit in
  Current state); the image smoke job (Decision 6) checks the server imports.
  The first release after this ships whatever the lock resolved on the day it
  was created — the delivery generates it fresh, so this is "today's newest",
  the same as a pip build today.
- **Developer environment**: an existing `.venv` created by `py -m venv` is
  replaced on the first `uv sync` (uv recreates a venv it did not create, or
  reuses it if compatible). One-time; noted in the PR description. The sync
  also drops anything installed there by hand, such as graphify, which moves
  first (Owner actions).

## Conventions

- Every comment in `requirements.txt` moves to `pyproject.toml` next to its
  entry; none is dropped or reworded beyond replacing "requirements.txt".
- Specifiers move verbatim (same floors, same pins, same caps). Nothing is
  upgraded or loosened in this change.
- Shell steps use `uv run <tool>`, never `.venv/bin/<tool>` or `source
  activate`, in CI. In CI each workflow syncs explicitly once per job and sets
  `UV_NO_SYNC: "1"` (Decision 5).
- Local scripts and docs use `uv run python -m <tool>` (Decision 8).
- uv is invoked with `--locked` in CI and Docker; never `--frozen` (which
  skips the consistency check) and never a bare `uv lock` in CI.

## Owner actions

- Right after merge, in `E:\Dev\TraxJourney` on main: run `uv sync`. Then
  the delivering session changes the local, untracked `dev-server.ps1` and
  `dev.ps1`: their uvicorn line becomes `uv run python -m uvicorn …`, and the
  `Activate.ps1` calls are dropped (U4 Do 5).

- Before the first `uv sync` in `E:\Dev\TraxJourney`, move graphify to its
  own venv (Decision 8). Steps:
  1. `py -3.14 -m venv $env:LOCALAPPDATA\graphify\venv`
  2. `& $env:LOCALAPPDATA\graphify\venv\Scripts\python -m pip install graphifyy`
  3. Add that `Scripts` directory to the user PATH.
  4. Check that `graphify --help` runs from a new shell.

  The delivering session may do this itself, since it is outside the repo
  and reversible, and reports it.
- After merge to main, in the repository's Insights → Dependency graph →
  Dependabot tab, run "Check for updates" once for each of the `uv`, `docker`
  and `github-actions` ecosystems. Confirm each job succeeds. Dependabot only
  runs from the default branch, so no unit can prove its configuration works
  (R2-1). If the `uv` job fails on `required-version`, or the `docker` job
  cannot list `ghcr.io/astral-sh/uv` tags, reopen this plan.

## Open decisions

None. Smart App Control was settled by the 2026-10-12 test (Decision 8). The
image job's path filter was settled at plan approval: path-filtered, with the
runtime-only import check covering code changes (Decision 6, R1-1).

## Execution units

### Wave 0 — the project definition

**U1 — `pyproject.toml`, `uv.lock`, `.python-version`**
- Goal: the dependency set is defined in `pyproject.toml` with the split of
  Decision 2 and locked in `uv.lock`.
- Scope: `pyproject.toml` (new), `uv.lock` (new), `.python-version` (new),
  `tests/test_python_packaging.py` (new), `tests/test_json_guard.py`.
- Context: `requirements.txt` in full (comments matter); Decisions 1-2 and
  Conventions. Example of a test that asserts on a repo file's text:
  `tests/test_rail_extract_workflow.py` (`_steps_text`, module-level loading).
  Read pyproject with `tomllib`.
- Do:
  1. Write `pyproject.toml`: `[project]` name `traxjourney`, version `0.0.0`
     (the release version lives in the git tag; say so in a comment),
     `requires-python = ">=3.14"`, dependencies; `[dependency-groups]` `rail`,
     `dev`, `release`; `[tool.uv] package = false`,
     `default-groups = ["dev", "release"]`,
     `required-version = ">=X.Y.Z"` (a floor, Decision 4). Move each comment.
  2. `.python-version` containing `3.14`.
  3. `uv lock` (on the dev machine or in a `python:3.14-slim` container with
     uv). Do not run `uv lock --upgrade` afterwards.
  4. `tests/test_json_guard.py:112` reads the FastAPI floor from
     `pyproject.toml`'s `[project.dependencies]` instead of `requirements.txt`.
  5. `tests/test_python_packaging.py`: runtime deps contain none of
     `pytest`, `pytest-cov`, `fakeredis`, `pyyaml`, `anthropic` (normalised
     names); `httpx`, `osmium`, `requests` are runtime; `rail` group is exactly
     osmium/requests/PyYAML with the same osmium/requests specifiers as
     runtime; `dev` includes `rail`; `anthropic` only in `release`;
     `default-groups` is `["dev", "release"]`; `required-version` is a
     `>=X.Y.Z` floor, not `==` (R2-1, R2-2: an exact pin blocks Dependabot and
     local uv); `sqlmodel` and `sqlalchemy` are `==` pins; `uv.lock` exists
     and its `requires-python` matches.
  6. Leave `requirements.txt` in place (U5 removes it).
- Acceptance: `uv lock --check` exits 0; `uv sync --locked` succeeds on a
  clean checkout in the py314 citest container; `pytest
  tests/test_python_packaging.py tests/test_json_guard.py` passes there; the
  whole suite passes there under `uv run pytest` with the same ignores as
  `test.yml`.
- Out of scope: upgrading or loosening any specifier; ruff/mypy config; any
  `[build-system]`.
- Latitude: none.
- Escalate if: `uv lock` cannot resolve with the current specifiers; the
  locked set makes any test fail that passes with pip today (report the
  package and versions — do not pin around it without asking); a file outside
  Scope is needed (X3).
- Depends on: —

### Wave 1 — parallel, disjoint files

**U2 — Dockerfile on `uv sync`**
- Goal: the image installs the locked runtime set with `uv sync` and still
  boots and runs its scripts unchanged.
- Scope: `Dockerfile`, `.dockerignore`, `tests/test_dockerfile.py` (new).
- Context: Decision 3, the Review envelope's runtime contract, `entrypoint.sh`,
  `worker-entrypoint.sh`, and the `docker compose run --entrypoint python`
  commands in `docs/DEPLOYMENT_VPS.md`. Example to follow: uv's own
  "Using uv in Docker" non-project-install pattern (copy binary from the
  pinned uv image, `uv sync --locked --no-install-project` before `COPY . .`).
  Keep the existing comments on the apt layers.
- Do:
  1. A named stage `FROM ghcr.io/astral-sh/uv:X.Y.Z AS uv` before the
     existing `FROM python:3.14-slim`, and `COPY --from=uv /uv /bin/uv` in the
     final stage. At delivery, use the same version as the `required-version`
     floor (Decision 4); afterwards the tag moves with Dependabot and the floor
     does not.
  2. `ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
     UV_NO_CACHE=1` (or a cache mount that leaves nothing in the layer) and
     `PATH="/app/.venv/bin:$PATH"`.
  3. `COPY pyproject.toml uv.lock .python-version ./` then
     `RUN uv sync --locked --no-default-groups`.
  4. `.dockerignore`: keep `.venv/` excluded; make sure `pyproject.toml`,
     `uv.lock`, `.python-version` are not excluded.
  5. `tests/test_dockerfile.py`: the Dockerfile has no `pip install`; it runs
     `uv sync` with `--locked` and `--no-default-groups`; the uv stage's
     `FROM` tag is an exact `X.Y.Z` (no `latest`, no floating major), and the
     version in `pyproject.toml`'s `required-version = ">=A.B.C"` is no newer
     than it (`A.B.C <= X.Y.Z`, compared as version tuples; R3-1: the floor
     does not have to follow the tag); `PATH` puts `/app/.venv/bin` first;
     the lock files are copied before `COPY . .`.
- Acceptance: `pytest tests/test_dockerfile.py` passes; locally
  `docker build -t tj-uv .` succeeds; in that image: `python -c "import
  api.router"` (with a dummy `SECRET_KEY` and SQLite `DATABASE_URL` if boot
  needs them), `python -c "import osmium"`, `python -c "import pytest"` fails,
  `alembic --help`, `gunicorn --version`, `rq --help`, and
  `python scripts/fetch_rail_data.py --help` all succeed; `entrypoint.sh`
  starts and `/api/version` answers on a scratch SQLite database. Paste the
  commands and outputs in the report.
- Out of scope: any stage beyond the uv one (no builder/runtime split);
  base-image change; touching the apt layers; `docker-build.yml`.
- Latitude: local design (cache mount vs `UV_NO_CACHE`, exact ENV layout).
- Escalate if: any entrypoint or documented `docker compose run` command
  needs changing; the image is more than 10% larger than today's (report both
  sizes); a file outside Scope is needed (X3).
- Depends on: U1

**U3 — CI on uv, image checks, Dependabot**
- Goal: every workflow installs Python dependencies with uv from the lock.
  Every PR proves the runtime-only set imports. PRs that touch the image build
  and smoke-test it. Dependabot keeps the lock, uv and the actions current.
- Scope: `.github/workflows/test.yml`, `.github/workflows/rail-extract.yml`,
  `.github/dependabot.yml` (new), `tests/test_rail_extract_workflow.py`,
  `tests/test_python_version_alignment.py`, `tests/test_ci_workflows.py` (new).
- Context: Decisions 4-7; `tests/test_rail_extract_workflow.py` in full
  (especially `:101` and `:626`) — it is the example for the new test file.
  `tests/test_python_version_alignment.py` in full: it currently requires
  `test.yml` to pin `python-version`, which this unit removes (R1-3).
  Keep every existing comment in the workflows; adjust only those that name
  pip or setup-python.
- Do:
  1. `test.yml` `test` job: replace `setup-python` + pip with
     `astral-sh/setup-uv@<major>` with `version: "X.Y.Z"`, the same exact
     version as the Dockerfile's uv stage (Decision 4); cache on `uv.lock`.
     Then `uv sync --locked`,
     `uv run alembic …`, `uv run pytest …` (same ignores). Workflow-level
     `env: UV_NO_SYNC: "1"` (Decision 5).
  2. `test.yml` `test` job, new step after the tests: the runtime-only import
     check of Decision 6 (R1-1). `UV_PROJECT_ENVIRONMENT=$RUNNER_TEMP/runtime
     uv sync --locked --no-default-groups`, then
     `$RUNNER_TEMP/runtime/bin/python -c "import api.router, src.jobs.worker"`
     and `$RUNNER_TEMP/runtime/bin/python scripts/fetch_rail_data.py --help`.
     Set the env vars an import needs (dummy `SECRET_KEY`, SQLite
     `DATABASE_URL`) the same way the image job does. The comment on the step
     says why: the suite runs with every group installed, so it cannot see a
     dev-only import.
  3. `test.yml` new `image` job per Decision 6, path-filtered. `paths:` only
     works for a whole workflow, so filter inside the job: a first step
     compares `git diff --name-only` against the base, and the build steps run
     only on a match (no third-party filter action). On `push` to main, always
     run.
  4. `rail-extract.yml`: workflow-level `env: UV_NO_SYNC: "1"`; `plan`/`build`
     use setup-uv (same `version:` as in step 1) + `uv sync --locked
     --no-default-groups --only-group rail`; `publish` uses `uv sync --locked
     --no-default-groups --group rail`; every `python …` step becomes
     `uv run python …`.
  5. `tests/test_rail_extract_workflow.py:626-629`: assert the publish job
     syncs the runtime set plus `rail` (not `--only-group`). Add: plan and
     build jobs sync `--only-group rail`. Keep `:101`'s real-run test as is.
  6. `tests/test_python_version_alignment.py`: re-express the property (CI
     Python == image Python) against the new single CI pin. `.python-version`
     equals the Dockerfile's `FROM python:X.Y`, and both match the X.Y of
     `requires-python`. Assert that no workflow pins its own `python-version`,
     so `.python-version` stays the only CI pin. Keep the file's docstring
     explaining why the property matters.
  7. `.github/dependabot.yml`: `uv` (directory `/`, weekly, group
     minor+patch), `docker` (directory `/`, weekly, `ignore` for
     `dependency-name: python` with `update-types:
     ["version-update:semver-minor", "version-update:semver-major"]`, R2-3)
     and `github-actions` (weekly, grouped). A comment on the `ignore` says
     why: a CPython minor is a deliberate change of three files.
  8. `tests/test_ci_workflows.py`:
     - no workflow contains `pip install` or `setup-python`;
     - every `setup-uv` step sets an exact `version: "X.Y.Z"` equal to the
       Dockerfile's uv stage tag (this exact equality is what makes a
       Dependabot uv bump fail until CI moves too, Decision 4);
     - every `uv sync` in a workflow has `--locked`;
     - both workflows set `UV_NO_SYNC: "1"` at workflow level;
     - the runtime-only import step exists in the `test` job and syncs with
       `--no-default-groups`;
     - dependabot has the `uv`, `docker` and `github-actions` ecosystems, and
       the `docker` one ignores `python` minor and major updates.
- Acceptance: the three test files pass in the citest container. The
  runtime-only import step is proven to catch the defect: temporarily add
  `import yaml` to a module under `src/`, run the step's commands locally in
  the citest container, and see them fail. Then remove the edit (by editing it
  back, not with `git checkout`) and see them pass; report both outputs.
  `actionlint` (or
  `docker run rhysd/actionlint`) is clean on both workflows; the PR's own CI
  run shows `test`, `image`, `flutter`, `android` green. `rail-extract.yml`
  is checked by its tests only (it is dispatch/schedule-run) — say so in the
  report.
- Out of scope: `docker-build.yml`, `android-release.yml`; auto-merge of
  Dependabot PRs; any change to rail logic.
- Latitude: local design (path filter mechanics).
- Escalate if: the `image` job needs secrets; a file outside Scope is needed
  (X3).
- Depends on: U1 (U2 for the `image` job to go green on CI; integrate U2
  first).

**U4 — Local dev scripts and docs**
- Goal: a developer sets up and runs the project with uv alone, and no doc or
  script tells them to use pip or create a venv by hand.
- Scope: `README.md`, `PROJECT_STRUCTURE.md`, `docs/RELEASING.md`,
  `docs/DEPLOYMENT_VPS.md`, `dev-server.ps1`, `dev.ps1`,
  `launch flutter only.bat`, `deploy.ps1` (messages and comment only),
  `bump_version_and_release.ps1`, `scripts/release_notes.py` (the hint at
  `:545` only), `scripts/make_video_icons.py` (docstring only),
  `.claude/settings.json` (the two pip/pytest allow rules only).
- Context: Decision 8 (`python -m` locally, and why) and Owner actions
  (graphify must have moved before this unit's first `uv sync`). Example of
  the doc tone: `docs/RELEASING.md` as it stands.
- Do:
  1. README backend setup: install CPython 3.14 and uv (links), `uv sync`,
     `uv run alembic upgrade head`, `uv run python -m uvicorn …`. Add one
     sentence: on Windows with Smart App Control, run tools as
     `uv run python -m <tool>`, because uv's `.exe` launchers are unsigned and
     get blocked. Add a second sentence: if uv says it is older than the
     project's `required-version`, run `uv self update` (R2-2). No pyenv.
  2. `PROJECT_STRUCTURE.md`: `pyproject.toml` / `uv.lock` / `.python-version`
     in place of `requirements.txt`.
  3. `docs/RELEASING.md`: `anthropic` is in the `release` group, installed by
     `uv sync`, never in the image; drop the `pip install anthropic` line.
  4. `docs/DEPLOYMENT_VPS.md:563`: the `.venv` from `uv sync`, else `python`.
  5. `dev-server.ps1` / `dev.ps1`: `uv run python -m uvicorn …` instead of
     activating `.venv`. *Delivery note:* both files are gitignored and
     untracked (8e7ca151 untracked them because they hold local env vars), so
     no unit can change them. Moved to Owner actions: the delivering session
     edits the local copies right after merge (owner decision, 2026-10-12).
  6. `launch flutter only.bat`: drop the dead venv activation block (it
     starts no Python) — **only if** nothing below it uses Python; otherwise
     change its message to `uv sync`.
  7. `deploy.ps1`: the "none found" message and comment say `uv sync`.
  8. `bump_version_and_release.ps1`: `& uv run python @notesArgs`; update the
     prerequisites comment (`uv`, not python on PATH; `-Polish` needs only
     `uv sync` + the key).
  9. `scripts/release_notes.py:545`: `run \`uv sync\` to enable --polish`.
  10. `scripts/make_video_icons.py`: `pip install -q cairosvg` →
      `uv run --with cairosvg …` or keep the throwaway container with
      `pip` replaced by `uv pip install --system` — local design, one line.
  11. `.claude/settings.json`: replace the pip rule with `Bash(uv sync:*)` and
      the pytest rule with `Bash(uv run python -m pytest tests/ -q)`.
- Acceptance: on the dev machine, from a clean clone in a temp dir:
  - `uv sync` reports it used the installed CPython 3.14, not a download;
  - `.\dev-server.ps1` serves `/api/version`;
  - `uv run python scripts/release_notes.py --help` works;
  - `deploy.ps1`'s Python probe passes against the new `.venv`: run its probe
    line by hand, not a deploy;
  - `git grep -nE "pip install|python -m venv|requirements\.txt"` over the
    Scope files returns nothing;
  - `graphify --help` still runs (Owner actions done).
- Out of scope: historical docs (Decision 9); `.claude/settings.local.json`
  (personal); rewriting the dev scripts beyond the Python invocation.
- Latitude: local design (items 6, 10).
- Escalate if: `deploy.ps1` needs more than message changes; a file outside
  Scope is needed (X3).
- Depends on: U1

### Wave 2 — remove the old path

**U5 — Delete `requirements.txt`, guard against its return**
- Goal: there is one dependency definition, and the repo cannot drift back to
  pip.
- Scope: `requirements.txt` (delete), `tests/test_python_packaging.py`,
  `scripts/fetch_rail_data.py` and `tests/test_rail_data_fetch.py`,
  `tests/test_rail_extract.py` (comments naming `requirements.txt` only).
- Context: U1's test file; Decision 9's list of historical files.
- Do:
  1. `git rm requirements.txt`.
  2. Update the three code comments that name `requirements.txt` to name
     `pyproject.toml`.
  3. Add to `tests/test_python_packaging.py`: `requirements.txt` does not
     exist; `git ls-files` (or a walk of the tree, excluding `.git`, `.venv`,
     `docs/`, `graphify-out/`, `tools/`) finds no `pip install -r` and no
     `requirements.txt` outside the historical documents. (The uv version
     agreement is U2's and U3's; do not duplicate it.)
- Acceptance: whole suite green under `uv run pytest` in the citest
  container; CI green on the integrated branch.
- Out of scope: historical docs.
- Latitude: none.
- Escalate if: anything still reads `requirements.txt` at run time; a file
  outside Scope is needed (X3).
- Depends on: U1, U2, U3, U4

## Definition of done

- `requirements.txt` is gone; `pyproject.toml` and `uv.lock` define and lock
  every Python dependency; `uv lock --check` passes.
- The image is built with `uv sync --locked --no-default-groups`; it contains
  no pytest, pytest-cov, fakeredis or PyYAML; `entrypoint.sh`,
  `worker-entrypoint.sh` and the documented `docker compose run --entrypoint
  python scripts/…` commands work unchanged.
- No workflow uses `setup-python` or `pip install`; every `uv sync` in CI and
  Docker is `--locked`, and CI never re-syncs after it (`UV_NO_SYNC`).
- Every build pins uv exactly and identically: the Dockerfile's uv stage tag
  and every setup-uv `version:`; a test fails if they differ.
  `required-version` is a floor that stays put unless moved deliberately; a
  test fails only if it is newer than the build's uv. Neither Dependabot nor
  the owner's local uv is refused after a uv bump.
- `.python-version` is the only CI Python pin and equals the image's Python;
  a test fails if they diverge.
- Every PR imports the server and the worker from a runtime-only environment;
  a PR that changes the Dockerfile or the lock builds and smoke-tests the
  image in CI.
- `.github/dependabot.yml` opens weekly uv, docker (uv stage only, no Python
  minor bumps) and GitHub Actions update PRs, and the owner has seen each
  ecosystem's first "Check for updates" succeed (Owner actions).
- The README sets up a backend with CPython 3.14 and uv only; local scripts
  run tools as `python -m`; no maintained doc or script tells anyone to
  `pip install` or create a venv by hand.
- graphify runs from its own venv outside the repo and survives `uv sync`.
- The full test suite passes in CI and in the py314 citest container.
- `graphify update .` run on main after merge (not from a worktree).
