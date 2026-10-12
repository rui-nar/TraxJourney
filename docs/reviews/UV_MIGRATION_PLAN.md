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
