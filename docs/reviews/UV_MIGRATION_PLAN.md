# Review ledger — Move to uv (#603)

Subject: docs/UV_MIGRATION_PLAN.md
Envelope: plan section "Review envelope" + REVIEW.md defaults

## Round 1 — 2026-10-12, reviewed at 48029e8f (plan uncommitted)

### R1-2 — Smart App Control blocks uv's venv launchers; the plan leaves it open and the README invites a managed Python
- Trigger: owner runs `uv sync` then `.\dev-server.ps1` (`uv run uvicorn …`) or `uv run pytest` on the SAC dev machine → `.venv\Scripts\uvicorn.exe` / `pytest.exe` are unsigned → "An Application Control policy has blocked this file".
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Note: the reviewer's broader claim (the venv's `python.exe` is a blocked trampoline, so `deploy.ps1` and `python -m` break too) was refuted on the dev machine on 2026-10-12. `uv venv --python 3.14` over the system CPython copies a signed `python.exe` (Authenticode Valid), and it runs. Only the console-script launchers (`pytest.exe`, NotSigned) are blocked. A uv-downloaded managed Python would not be signed, so it is not safe to assume either.
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (plan: Decision 8 `python -m` locally; README requires an installed CPython 3.14; U4 acceptance checks no download and the deploy.ps1 probe; Open decision closed)

### R1-3 — tests/test_python_version_alignment.py fails once setup-python leaves test.yml, and no unit owns it
- Trigger: U3 implementer swaps setup-python for setup-uv → the test finds no `python-version` pin in test.yml → CI red; the file is outside every Scope (X3).
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (plan: U3 Scope + Do 6 re-express test_python_version_alignment.py on .python-version)

### R1-4 — First `uv sync` uninstalls graphify from the project .venv
- Trigger: owner runs `uv sync` → uv removes packages not in uv.lock, including the pip-installed `graphifyy` → `graphify update .` (CLAUDE.md, plan DoD) fails.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (.venv\Scripts\graphify.exe present; uv sync is exact)
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (plan: Decision 8 + Owner actions move graphify to its own venv before the first uv sync; U4 acceptance checks it)

### R1-5 — `uv run python …` in the rail jobs re-syncs to the default groups
- Trigger: monthly rail-extract run → `plan`/`build` sync `--only-group rail`, then `uv run python …` re-syncs with runtime + dev + release → the rail group is dead and every matrix leg does a full install; nothing reports it.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan: Decision 5 + Conventions, workflow-level UV_NO_SYNC=1 asserted by tests/test_ci_workflows.py)

### R1-6 — Dependabot as configured never bumps the uv pins
- Trigger: weekly Dependabot run → github-actions bumps `uses:` refs, not setup-uv's `version:` input; nothing covers the Dockerfile's `COPY --from=ghcr.io/astral-sh/uv:X.Y.Z` → uv stays at its first pin indefinitely; the agreement test of Decision 4 never fires.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan: Decision 4 single pin in [tool.uv] required-version read by setup-uv version-file; uv as a named FROM stage; dependabot docker ecosystem; a test that the tag equals required-version)

### R1-1 — Path-filtered image smoke job misses a dev-only import added to server code
- Trigger: a developer imports PyYAML (dev group) from api/ or src/ without declaring it at runtime → CI test job is green, image job skipped by the path filter → release image fails at boot with ModuleNotFoundError.
- Scores: trigger=plausible (triager corrected from concrete: needs a developer mistake), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a non-test module imports a package outside [project.dependencies]; any release/val image fails to boot with ModuleNotFoundError.
- Override: user: Fix now — close it with the cheap runtime-only import check on every PR rather than defer.
- Outcome: fixed (plan: Decision 6 runtime-only import check; U3 Do 2, with acceptance proving it fails on a planted `import yaml`)

## Round 2 — 2026-10-12, reviewed at 48029e8f (plan uncommitted; round-1 fixes only)

Envelope question raised: does the envelope's "setup-uv pinned to an exact version" mean the action's `uses:` ref must be a commit SHA, or does pinning the uv binary satisfy it? Owner (2026-10-12): the uv binary version is pinned exactly; action refs keep the repo's major-tag convention. Plan envelope reworded.

### R2-1 — An exact `required-version` also binds Dependabot's uv, so its weekly uv lock updates fail
- Trigger: Dependabot's weekly uv run → `uv lock --upgrade-package …` with its bundled uv, which is not X.Y.Z → uv refuses ("Required uv version … does not match") → no lock-bump or security PR is ever opened; the error appears only in the Dependabot tab.
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan: Decision 4 required-version becomes a >= floor; exact pins only in the Dockerfile uv stage and setup-uv `version:`; three-way agreement test; post-merge Owner action runs Dependabot once per ecosystem)

### R2-2 — The exact uv pin locks the owner's local uv to the repo's; the first merged bump blocks local `uv run`, the release script included
- Trigger: owner merges a uv bump PR → runs `bump_version_and_release.ps1` / `dev-server.ps1` with local uv 0.13.0 → "Required uv version … does not match" → release blocked, no recovery step documented.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan: same floor change; README recovery line `uv self update`, U4 Do 1)

### R2-3 — Dependabot `docker` cannot give `python:3.14-slim` patch digests; it opens a 3.15-slim bump the alignment test rejects
- Trigger: first weekly Dependabot docker run → `FROM python:3.14-slim` has no digest → Dependabot proposes 3.15-slim → test_python_version_alignment fails → a red PR to close by hand, at every CPython minor.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified (Dockerfile:1 has no @sha256)
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan: Decision 7 + U3 Do 7/8 dependabot docker ignores python semver-minor/major; purpose stated as the uv stage)

## Round 3 — 2026-10-12, reviewed at 48029e8f (plan uncommitted; round-2 fixes only)

Envelope question raised: the "Build reproducibility" bullet says two builds of one commit install the same packages, while Decision 7 relies on `python:3.14-slim` floating to the latest 3.14 patch. Is the base image (CPython patch, apt packages) outside that bullet, i.e. "packages" = Python packages from the lock? Owner (2026-10-12): yes — the base image floats on purpose so its security patches arrive; plan envelope reworded.

### R3-1 — The three-way equality drags the `required-version` floor up at every uv bump, reopening R2-1 (and R2-2)
- Trigger: Dependabot docker proposes uv X.Y.Z+1 → the agreement test forces the floor to `>=X.Y.Z+1` in the same PR → after merge, Dependabot's bundled uv (which lags upstream) is refused on the floor, so no lock/security PRs open until dependabot-core catches up; the owner's local uv is refused until `uv self update`. Recurs at every bump.
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7) — new evidence that the R2-1 fix does not hold, not a duplicate (D2)
- Override: —
- Outcome: fixed (plan: Decision 4 tests tie Dockerfile tag == setup-uv versions exactly and only floor <= tag; floor stays at the delivery version; U2 Do 1/5, U3 Do 8, DoD reworded)

## Delivery

Branch `chore/603-uv`, orchestrator worktree `E:/Dev/TraxJourney-603`, unit worktrees made by hand (agent worktree isolation breaks on the e:/E: drive-letter case).

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | pyproject.toml, uv.lock, .python-version | Sonnet | — | 1 | — | yes | R2-1, R2-2, R3-1 |
| U2 | Dockerfile on `uv sync` | Opus | S2 | 1 | — | yes | R1-6, R3-1 |
| U3 | CI on uv, image checks, Dependabot | Opus | S3 | 2 | owner decision (setup-uv has no major tags after v7 → `@v10.2.0`) | yes | R1-1, R1-3, R1-5, R1-6, R2-3, R3-1 |
| U4 | Local dev scripts and docs | Opus | S3 | 1 | owner decision (dev-server.ps1/dev.ps1 untracked → post-merge owner action) | yes | R1-2, R1-4, R2-2 |
| F1 | Pin uv 0.12.19 (0.13.0 was 3 days old, against Decision 4) | Sonnet | — | 1 | — | yes | — |
| U5 | Delete requirements.txt, guard against its return | Sonnet | — | 2 | — | no (guard could pass on an empty `git ls-files`; fixed in 64d67c9c) | — |
| F2 | Fix D1-1 (python-preference=system), D1-2 (guard without git), D1-3 (PyYAML comment) | Sonnet | — | 1 | — | yes | D1-1, D1-2, D1-3 |
| F3 | Fix D2-1..D2-3: decide git vs walk by `.git` existence | Sonnet | — | 1 | — | yes | D2-1, D2-2, D2-3 |

Checks: full suite in the py314 citest container with uv 0.12.19 — 7213 passed after wave 1 + F1, 7215 after U5, 7216 after F2 (39 skipped each); the 6 packaging/CI/Docker test files 136 passed after F3 (test-only change); alembic check clean. Image built, booted (/api/version 200), no pytest inside. PR CI (test, image, flutter, android) not yet run: it needs the PR.

Notes:
- F1 is an orchestrator fix unit: the plan named 0.13.0 without checking its release date; U3 caught it.
- Dev machine (owner action, done 2026-10-12): graphify moved to `%LOCALAPPDATA%\graphify\venv` (pinned 0.9.67) on the user PATH; the broken uv-tool graphify removed with owner approval. Side effect undone: running the newer graphify auto-refreshed the graphify skill; SKILL.md and the version marker restored, the refreshed copy kept as SKILL.md.0.9.86-autorefresh.

## Diff review round 1 — 2026-10-13, reviewed at befe4e35 (branch chore/603-uv vs origin/main)

### D1-2 — The pip-path guard needs a git index; it errors on a git-less archive copy
- Trigger: someone runs the suite from a `git archive` copy without `git init && git add` → `git ls-files` fails (check=True) → one test errors.
- Scores: trigger=plausible (triager corrected from concrete: the documented recipe, every run in this delivery and CI have an index), impact=cosmetic (corrected from degraded-ux), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — follows the convention of seven existing `git ls-files` tests
- Revisit when: a git-less archive (or the image, or an sdist) becomes the documented test recipe; then fix every `git ls-files` test with one shared skip-or-fallback helper.
- Override: user: Fix now — owner overrode all three diff-review deferrals (2026-10-13).
- Outcome: fixed (F2, 08d10149)

### D1-1 — uv's default `python-preference = managed` would put a uv-managed (unsigned) CPython ahead of the signed system one
- Trigger: a uv-managed CPython matching `.python-version` gets installed on the SAC dev machine (`uv python install`, another project, a minor bump before the system install) → `uv sync` builds .venv on it → SAC blocks `.venv\Scripts\python.exe` → local `uv run python …`, the release script and deploy.ps1's probe fail.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10) — not a duplicate of R1-2 (that fix covers the first sync only)
- Revisit when: `uv python list --only-installed` shows a uv-managed CPython on the dev machine, `.python-version` moves to a minor with no system install, or SAC blocks `.venv\Scripts\python.exe`; then add `[tool.uv] python-preference = "system"`.
- Override: user: Fix now — owner overrode all three diff-review deferrals (2026-10-13).
- Outcome: fixed (F2, 08d10149)

### D1-3 — The `rail` group's PyYAML comment says "Test-only … Nothing at runtime imports it"
- Trigger: a maintainer reads the comment and moves PyYAML to `dev` → the rail-extract jobs (build_rail_extract.py, route_corpus.py import yaml) would break; test_rail_group_is_exactly_osmium_requests_and_pyyaml catches it in PR CI first.
- Scores: trigger=plausible, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the next change touching the rail group or PyYAML's placement; correct the comment there.
- Override: user: Fix now — owner overrode all three diff-review deferrals (2026-10-13).
- Outcome: fixed (F2, 08d10149)

## Diff review round 2 — 2026-10-13, reviewed fix commit 08d10149 (F2)

### D2-1 — The pip guard's tree-walk fallback ignores .gitignore
- Trigger: full checkout run without a git binary (e.g. bind-mounted into a git-less container) → the walk scans gitignored `.claude/worktrees/*` (old copies with `pip install -r requirements.txt`), `data/`, `web_client/`, `*.osm.pbf` → false offenders, large files read whole.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the suite runs on a full checkout without git, or the walk flags gitignored paths; apply the shared ROOT/.git fix.
- Override: user: Fix now — option 1: decide git vs walk by ROOT/.git existence (2026-10-13).
- Outcome: fixed (F3, 28060976)

### D2-2 — The not-a-repo detection matches English stderr text
- Trigger: archive copy tested where git prints translated messages → "not a git repository" not found → RuntimeError instead of the walk.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: an archive copy is tested under a non-English git; apply the shared ROOT/.git fix.
- Override: user: Fix now — option 1: decide git vs walk by ROOT/.git existence (2026-10-13).
- Outcome: fixed (F3, 28060976)

### D2-3 — An archive copy inside another repo's work tree gets an empty `git ls-files`
- Trigger: archive extracted under an unrelated git work tree → `git ls-files` exits 0 with nothing → the non-empty assertion fails instead of walking.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: an archive copy is extracted inside another work tree, or the "no candidate files" check fires on one; apply the shared ROOT/.git fix.
- Override: user: Fix now — option 1: decide git vs walk by ROOT/.git existence (2026-10-13).
- Outcome: fixed (F3, 28060976)

## Diff review round 3 — 2026-10-13, reviewed fix commit 28060976 (F3)

Round 3 confirms D2-1, D2-2 and D2-3 closed by option 1.

### D3-1 — With a `.git` that git refuses to read, the guard errors without git's reason, where four sibling helpers skip
- Trigger: suite run on a copy whose `.git` git rejects (dubious-ownership bind mount, or a worktree's `.git` file pointing at a Windows gitdir after copying into Linux) → `check=True` raises CalledProcessError, uncaught → the test errors with "exit status 128" and no message.
- Scores: trigger=plausible, impact=cosmetic (triager corrected from wrong-visible: no false pass or false offender), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10). Round cap reached: a Fix now would need a fourth round, which only the owner can ask for.
- Revisit when: the guard errors with "exit status 128" anywhere, or the next change to the git ls-files helpers; then `except (OSError, subprocess.CalledProcessError)` → pytest.skip with the decoded stderr, ideally in one shared helper (see D1-2).
- Override: —
- Outcome: open
