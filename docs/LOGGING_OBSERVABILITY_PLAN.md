# Logging & Observability — Plan for #205

Status: **plan only, nothing implemented yet.** This document is the handoff
artifact for implementation — written so each numbered unit below can be
given to a separate agent/session to execute independently. Read the whole
"Conventions" section before starting any unit; it's the shared contract
every unit must follow so the parts compose without a second integration
pass.

## 1. Problem statement

Issue #205: "Logging is extremely useless... any failure is impossible to
debug." The framework (`src/utils/logging.py`: `configure_logging()` /
`get_logger()`, wired to `api.*`/`src.*`/`apscheduler`, console handler with
millisecond timestamps) already works and is well tested
(`tests/test_logging.py`). The actual problem is adoption: only **4**
`logger.*()` calls exist in the whole repo against **~269** `except`/`raise`
sites in `api/` alone, plus two structural gaps the framework never had
(request/user correlation, and log persistence across deploys).

Additional requirement from discussion: with several concurrent users, we
need to reconstruct **what one user did, including the successful steps,
leading up to a failure — without asking them.** This means logging normal
calls (internal API + external integrations), not just failures, and tagging
every log line with enough identity to filter one user's/one request's
timeline out of everyone else's concurrent traffic.

## 2. Audit — where the silence is (reference for unit authors)

Full catalog from a repo-wide sweep of `api/`, `src/`, `models/`. Use this to
avoid re-deriving file:line locations; each unit below already scopes its
own slice of this list.

**Modules with zero logging at all:**
- `src/api/strava_client.py` — no logger import
- `src/api/polarsteps_client.py` — no logger import
- `src/services/hafas_service.py` — no logger import; its only caller
  (`api/segments.py:94-95`) also swallows silently
- `src/tile_renderer.py` — no logger import
- `src/auth/token_store.py` — no logger import (local CLI token cache, low
  blast radius but still silent)

**Duplicated silent-drop bug** (same root cause, 5 call sites — fix once,
apply everywhere): `Activity.from_strava_api(raw)` wrapped in
`except Exception: pass` at `api/activities.py:124-125,178-179,226-227`,
`api/strava.py:355-356,458-459`, `api/projects.py:589-590`,
`src/project/project_io.py:219-222`.

**User-visible content silently disappears, no trace:**
`api/journal.py:168-169` and `api/memories.py:512-513`
(`_download_photo_from_url`), `src/poster/poster_renderer.py:341-345`
(`_paste_cover`), `src/tile_renderer.py:240-241` (`_do_prerender`).

**Client-only error, never server-logged** (`except: raise HTTPException(...,
detail=str(exc))` with no `_log` call): `api/polarsteps.py:127-131,189-190,216-217`,
`api/people.py:445-446,469-470`, `api/geo.py:239-240`,
`api/backup.py:35-36` (disaster-recovery restore path — highest severity in
this group).

**Security-relevant, unlogged:** `api/deps.py` — `decode_token`/
`require_admin` never log expired/invalid/tampered tokens
(`api/deps.py:84-91,133-136`).

**Already well-instrumented — do not touch beyond what each unit specifies:**
`src/billing/stripe_gateway.py`, `src/services/overpass_service.py` (one
minor gap noted in Unit C), `api/router.py`'s existing exception handlers,
`src/billing/usage.py`.

## 3. Conventions (binding on every unit)

- **Logger acquisition:** always `from src.utils.logging import get_logger;
  _log = get_logger(__name__)`. Never bare `logging.getLogger(__name__)` —
  three existing files do this (`api/poster.py`, `src/email/service.py`,
  `src/tile_renderer.py`); fix on touch, don't go looking for others.
- **Levels:**
  - `DEBUG` — verbose internals; chatty/polled routes' access-log line (see
    Unit 0.2's allowlist).
  - `INFO` — normal lifecycle: a sync/import completed with counts, a job
    ran, a request completed (non-chatty route), an external call
    succeeded.
  - `WARNING` — degraded-but-handled: retried, fell back, partial-batch
    failure, cache forced a refetch.
  - `ERROR` / **`.exception()`** — something failed and the user got a worse
    outcome than they should have. Always `.exception()` (not `.error()`)
    inside an `except` block — captures the traceback. This is the single
    most common review mistake to watch for.
- **Format is logfmt-shaped, not free text.** `configure_logging()`'s
  formatter changes from `%(name)s - %(levelname)s - %(message)s` to a
  logfmt preamble (`request_id=%(request_id)s user_id=%(user_id)s ...`,
  populated by a `logging.Filter`) followed by the human-readable message.
  Reason: Loki indexes labels, not message content, and `request_id`/
  `user_id` must **never** become Loki labels (unbounded cardinality) — they
  have to be parseable out of the message body at query time via
  `| logfmt`. Keep it human-grep-able too; this isn't JSON.
- **Redaction:** never log JWTs, passwords, Stripe secret keys, E2EE key
  material, raw request bodies. Email logging: recipient + subject only,
  never body (the `ConsoleEmailService` dev backend logging full body is
  fine — dev-only path).
- **`src/utils/metrics.py:track_external()`** is the single choke point for
  external-call logging (it already wraps Strava/Polarsteps/Translate/SMTP
  for metrics). Unit G upgrades it to also log; every other unit that makes
  external calls should already be using `track_external` or adopt it as
  part of adding logging — don't hand-roll a second pattern.
- **Chatty routes → `DEBUG`, not silence.** Read-only + polled-or-per-paint:
  poster/job-status polling, tile serving, `GET /api/geo/project` (+
  low-res), `GET /api/version`, `/metrics`. Everything else — writes, auth,
  sync/import, external calls — `INFO`. Implemented as a hardcoded
  path-template allowlist checked in the access-log middleware (Unit 0.2),
  **not** a per-route decorator — keeps this in one file instead of N.
- **Every unit adds tests** (`caplog`-based) asserting the *specific*
  failure path actually emits a log at the right level — not just that
  behavior is unchanged. Add to the existing test file for that module if
  one exists; otherwise create `tests/test_<module>_logging.py`. Don't add
  to another unit's test file.

## 4. Observability infrastructure (decided, out of app-code scope)

- **Topology:** VPS runs the app + Grafana **Alloy** only (not Promtail —
  deprecated in favor of Alloy). NAS runs Loki + Prometheus + **Grafana**,
  colocated, generous disk, no CPU contention with prod. Already-deployed
  **Dozzle** (`/opt/dozzle`, VPS, port 8892) keeps covering live-tail — no
  new work needed there; worth a one-time check that it isn't reachable
  without auth on a public port (Dozzle has none built in), but that's a
  verification, not a build item.
- **Not Mimir.** Mimir (Prometheus-compatible long-term/multi-tenant
  storage) is more than this scale needs — one app, a handful of concurrent
  users. Grafana already takes Prometheus and Loki as two plain datasources;
  that gets the "everything in one Grafana" outcome without a new storage
  component with its own ring/config. Revisit only if this genuinely needs
  to scale multi-tenant later — the migration path (repoint Alloy's
  `remote_write`) is cheap to defer.
- **Push, not pull, for metrics too.** Alloy scrapes `127.0.0.1:8000/metrics`
  over loopback and `remote_write`s to NAS Prometheus
  (`--web.enable-remote-write-receiver`). Once confirmed working, `/metrics`
  stops needing to be internet-reachable at all — drop its bearer-token
  Caddy exposure (`docs/METRICS.md`'s current model). Net security
  improvement, not just a refactor.
- **VPS↔NAS link: Tailscale (or WireGuard) private overlay, not a
  port-forward + DDNS + bearer token.** The NAS has never had an inbound
  port opened for anything but SSH; a second internet-facing ingestion
  endpoint there is a materially different risk than the VPS's hardened
  setup. Loki/Prometheus never touch the public internet under this design.
- **Alloy needs a bounded local WAL + explicit retry/drop policy** for NAS
  or tunnel downtime (home power cut, DSM update reboot) — don't let a
  prolonged outage fill VPS disk or block the app.
- **Keep local VPS log rotation regardless of Loki.** Loki is additive, not
  a replacement — if the NAS or tunnel is down during an incident, `docker
  compose logs` on the VPS must still answer "what just happened."
  `docker-compose.yml.example` currently has no `logging:` driver config at
  all (unbounded default) — needs `max-size`/`max-file` either way.
- **Grafana Alerting replaces the earlier idea of adding Sentry.** Alert on
  Loki (`rate of level=error`) or Prometheus thresholds, self-hosted, no
  third-party data egress — better fit for a product built around
  zero-knowledge E2EE than shipping stack traces to an external service.
  Drop Sentry from scope entirely.
- **Adjacent opportunity, not in scope for #205:** `docs/DEPLOYMENT_VPS.md`'s
  open item *"Off-site backups independent of OVH"* is still unchecked — the
  same Tailscale tunnel could carry the nightly SQLite backup rsync to the
  NAS. Worth doing in the same infra pass since the tunnel is being built
  anyway, but call it out as a separate commit, not bundled into #205.
- **Verify before relying on it:** NAS RAM/CPU headroom (not documented
  anywhere I've read — only VPS specs are known: 2 vCore/4GB/40GB), and NTP
  on both hosts (default on Debian/DSM, but cross-host log/metric
  correlation by timestamp silently breaks if clocks drift).

## 5. Open decisions (need an answer before or at implementation, not blocking the plan itself)

1. **VPS log file persistence** — rotating file handler under a bind mount
   (survives container recreation on deploy) vs. accept console-only and
   lean entirely on Loki once it exists. Affects Unit 0.2 and H1's scope.
2. **Slow-request WARNING threshold** in the access-log middleware — default
   proposal 2s, but poster rendering and Overpass calls are legitimately
   slow; may need a per-route override rather than one global number.
3. **Auth-failure log level/detail** in Unit F — log tampered/malformed
   tokens distinctly from plain-expired (routine, high-volume, not
   suspicious) so the signal doesn't drown in noise on a public instance.

## 6. Execution units

Each unit lists: **scope** (files — disjoint from every other unit unless
noted), **context** (what to read first), **do**, **acceptance criteria**.
Units in the same wave touch no common files and can run fully in parallel.
Waves are sequenced for *value* (later waves are more useful once earlier
ones exist), not because of hard code blocking — file ownership is disjoint
throughout, so nothing here actually prevents starting every unit at once if
you have the parallelism for it.

### Wave 0 — foundation

**Unit 0.1 — `docs/LOGGING.md`**
- Scope: new file only.
- Do: transcribe Section 3 of this document into a standalone conventions
  reference, written for someone touching a route/service file who needs
  the level/format/redaction rules without reading this whole plan.
- Acceptance: exists, matches Section 3 exactly (this doc is the source of
  truth if they ever diverge — update both together).

**Unit 0.2 — Correlation core + access logging**
- Scope: `src/utils/logging.py`, `api/router.py`, optionally a new
  `api/middleware.py`.
- Context: read `src/utils/logging.py` in full first — `configure_logging()`
  and the `_APP_LOGGER_NAMES`/`_UVICORN_LOGGER_NAMES` split matter for where
  the new filter attaches. Read `api/router.py`'s existing
  `@app.exception_handler` blocks (`StaleWriteError`, `QuotaExceeded`,
  `AuthenticationError`, `APIError`) — there is currently **no** catch-all
  for an uncaught exception; FastAPI's default 500 handling is the only
  thing catching those today, silently.
- Do:
  1. `contextvars.ContextVar` for `request_id` (minted per request, short
     random id) and `user_id` (set once `get_current_user`/`decode_token`
     resolves; unauthenticated routes carry `request_id` only).
  2. `logging.Filter` injecting both into every `LogRecord`, attached
     wherever `configure_logging()` attaches its handler.
  3. Reformat per Section 3's logfmt convention.
  4. Access-log middleware: one line per request at `INFO` (or `DEBUG` per
     the chatty-route allowlist — hardcode the list from Section 3) with
     route template, status, duration, `request_id`, `user_id`.
  5. `X-Request-Id` response header; include `request_id` in any 4xx/5xx
     JSON body (touches the existing exception handlers plus a new
     catch-all that logs `.exception()` before returning a generic 500).
- Acceptance: `caplog` tests — filter injects both vars correctly under
  concurrent requests (two interleaved async requests never see each
  other's `request_id`); DEBUG routes don't emit at INFO; a raised
  `HTTPException` and an uncaught exception both produce a log line
  containing the same `request_id` returned in the response.

### Wave 1 — fully parallel, disjoint files

**Unit A — Strava: client + the 5-site parse-drop bug**
- Scope: `src/api/strava_client.py`, `api/activities.py`, `api/strava.py`,
  `api/projects.py`, `src/project/project_io.py`, one new shared helper
  (e.g. a function in `src/models/activity.py` or a new
  `src/utils/activity_ingest.py` — implementer's call, document the choice
  in the commit).
- Context: Section 2's "duplicated silent-drop bug" list has exact
  line numbers for all 5 sites.
- Do: add `get_logger(__name__)` to `strava_client.py`, log its
  retry/refresh/token-clear failures at `WARNING`/`.exception()` as
  appropriate; wrap its Strava HTTP calls in `track_external` if not
  already (check — some already are). Write one shared helper,
  `parse_activities_or_log(raw_list, source: str) -> list[Activity]`,
  that does the try/except once and logs a single `WARNING` per call with
  `source` + count dropped (not one line per bad activity — avoid spam on
  a large import). Replace all 5 sites with calls to it.
- Acceptance: tests for the client's logged failure paths; one test per
  call site (or a parametrized test across all 5) confirming a malformed
  activity in the input produces exactly one `WARNING` log line with the
  right `source` and count, and the valid activities still come through.

**Unit B — Polarsteps: client + api/polarsteps.py + api/people.py**
- Scope: `src/api/polarsteps_client.py`, `api/polarsteps.py`,
  `api/people.py`.
- Context: this integration currently has **zero** server-side log lines
  anywhere in its call chain (Section 2) — every failure only reaches the
  one user who hit it via `HTTPException.detail`.
- Do: `get_logger(__name__)` in the client, log fetch/parse failures.
  In `api/polarsteps.py:127-131,189-190,216-217` and
  `api/people.py:445-446,469-470`: keep the existing `HTTPException` (client
  still needs an answer) but add a `_log.warning`/`.exception()` call before
  raising — don't just replace one with the other. Wrap client HTTP calls in
  `track_external("polarsteps", ...)` if not already.
- Acceptance: a simulated Polarsteps failure (timeout/5xx/malformed JSON)
  produces both the expected `HTTPException` *and* a log line, per route.

**Unit C — HAFAS + Overpass + api/segments.py**
- Scope: `src/services/hafas_service.py`, `src/services/overpass_service.py`
  (only the `_find_station_near` gap, line ~220-221 — don't touch the rest,
  it's already well-instrumented), `api/segments.py`.
- Context: `hafas_service.py` has no logger at all, and its sole caller
  (`api/segments.py:94-95`, `except HafasError: pass`) also swallows —
  every HAFAS failure in the app is currently a complete blackout, silently
  degrading to straight-line geometry.
- Do: `get_logger(__name__)` in `hafas_service.py`, log failures at
  `WARNING`, wrap its outbound calls in `track_external("hafas", ...)`.
  Fix `api/segments.py:94-95` to log the degrade-to-straight-line decision
  at `WARNING` with segment/project context before falling back. Add a
  `_log.info` (or keep the existing warning, check current code) to
  `overpass_service.py:_find_station_near` so the detailed error
  `_overpass()` already builds isn't dropped.
- Acceptance: a failing HAFAS lookup produces a log line and still returns
  the straight-line fallback geometry (behavior unchanged, visibility added).

**Unit D — Tiles + poster rendering**
- Scope: `src/tile_renderer.py`, `src/poster/poster_renderer.py`.
- Context: `tile_renderer.py` has no logger; `poster_renderer.py` is
  otherwise well-instrumented (`_photo_resolver`, basemap failures already
  log) except `_paste_cover` (line ~341-345), which is the one place a
  paying customer's poster print can come out with a missing photo and
  nothing in the logs explains it.
- Do: `get_logger(__name__)` in `tile_renderer.py`, log
  `_do_prerender`'s (line ~240-241) failure at `WARNING` (keep it
  non-fatal — the docstring's "never crash the background thread" intent
  stays, just stop being silent about it). Add the missing log call to
  `_paste_cover`.
- Acceptance: a corrupt/unreadable photo fed to `_paste_cover` still
  produces a blank card slot (unchanged) but also a `WARNING` log
  naming which photo and which poster job.

**Unit E — Background content loss + backup restore**
- Scope: `api/journal.py`, `api/memories.py`, `api/backup.py`.
- Context: `_download_photo_from_url` in both journal and memories
  (line ~168-169 / ~512-513) silently drops a failed photo fetch with
  `except Exception: return`. `api/backup.py:35-36`'s restore path is the
  highest-severity item in this unit — a failed disaster-recovery DB
  restore currently has no server-side record at all, only whatever the
  admin's browser shows.
- Do: add `.exception()`/`.warning()` at each drop point with enough
  context to reproduce (project id, entry id, source URL/path). For
  `api/backup.py`, log the restore attempt (which backup file, who
  triggered it) and its outcome regardless of success/failure — this is a
  rare, high-stakes operation; it should always leave a trail, not just on
  failure.
- Acceptance: tests confirming each of the 3 failure paths logs before
  returning/raising, including the backup-restore success case (not just
  failure — see "Do").

**Unit F — Auth-failure logging**
- Scope: `api/deps.py`.
- Context: `decode_token` (line ~84-91) and `require_admin` (line
  ~133-136) never log. Open decision #3 above applies here — implement
  with two distinct signals: plain-expired (routine, don't over-log —
  `DEBUG` or a rate-noticeable `INFO`, your call, document the reasoning
  in the commit) vs. malformed/tampered `sub` claim (`WARNING` — this is
  the one worth being able to spot as potential probing).
- Acceptance: test that an expired token, an invalid-signature token, and
  a malformed-claim token each produce a distinguishable log line/level.

**Unit G — `track_external` logging + remaining unwrapped integrations**
- Scope: `src/utils/metrics.py`, `src/billing/stripe_gateway.py`,
  `api/geo.py` (Nominatim `places` endpoint, line ~239-240 only).
- Context: `track_external` (metrics.py:~229) already wraps
  Strava/Polarsteps/Translate/SMTP for Prometheus metrics but never logs.
  Units A/B/C independently wrap their own integrations in it (or add new
  wraps) — this unit only needs to touch the integrations *not* already
  claimed by another unit: Stripe and Nominatim.
- Do: add a log call inside `track_external`'s `finally`/`except` — `INFO`
  on success (service, endpoint, duration), `WARNING` on failure — so every
  current and future caller gets call-level logging for free, no per-site
  changes needed elsewhere. Wrap `api/geo.py`'s Nominatim call in
  `track_external("nominatim", ...)` (it currently has no metrics or log
  coverage at all). Confirm `stripe_gateway.py`'s existing calls (already
  wrapped for Stripe SDK errors via `_log.warning` — see Section 2, "already
  well-instrumented") also get the new success-path `INFO` line for free
  once `track_external` itself is upgraded — no changes needed to
  `stripe_gateway.py` itself unless its calls aren't yet going through
  `track_external` (verify; wrap if not).
- Acceptance: `tests/test_metrics.py` (existing file — extend it) gets a
  `caplog` test confirming `track_external` logs both outcomes.

### Wave 2 — infrastructure, disjoint from all app code

**Unit H1 — VPS-side: Alloy + log rotation**
- Scope: `docker-compose.yml.example`, `docs/DEPLOYMENT_VPS.md`.
- Do: add an Alloy service definition (config scrapes `127.0.0.1:8000/metrics`
  + tails app container logs, ships to NAS Loki/Prometheus over the
  Tailscale/WireGuard address — placeholder/documented, not a working
  secret-bearing config committed to the repo). Add `logging:` driver
  block (`max-size`/`max-file`) to every service in the compose file —
  currently absent entirely (Section 4). Document the Tailscale setup steps
  in `docs/DEPLOYMENT_VPS.md`, and the plan to drop `/metrics`'s public
  Caddy exposure once Alloy's scrape is confirmed working.
- Acceptance: `docker compose config` validates; a fresh reader of
  `docs/DEPLOYMENT_VPS.md` can stand up the VPS side without asking a
  question the doc doesn't answer.

**Unit H2 — NAS-side + query reference**
- Scope: new `docs/OBSERVABILITY.md`.
- Do: documented (not committed as host-specific, mirroring how the VPS
  compose is gitignored) NAS-side compose sketch — Loki + Prometheus +
  Grafana, monolithic/filesystem storage, retention/compaction settings.
  Grafana datasource config (both Prometheus and Loki, both pointed at
  localhost-on-NAS). A handful of concrete LogQL/PromQL examples an
  operator would actually run — e.g. `{service="traxjourney"} | logfmt |
  user_id="123"` to pull one user's whole session across concurrent
  traffic, `{service="traxjourney"} | logfmt | request_id="..."` for one
  request end-to-end. 2-3 starter Grafana alert rules (error-rate spike,
  job failure) replacing the earlier Sentry idea. One line noting Dozzle
  is already deployed (`/opt/dozzle`, :8892) and covers live-tail — confirm
  it isn't reachable without auth on a public port.
- Acceptance: exists, self-contained enough that implementing it doesn't
  require re-reading this plan doc.

## 7. Definition of done

- Every module in Section 2's "zero logging" list has a logger and at least
  one exercised failure-path test.
- The 5-site Activity-parse duplication is a single call site.
- A request/user can be traced end-to-end via `request_id`/`user_id` across
  every log line it touches, including external calls it triggered, and
  that id is visible to whoever reports the bug.
- Debugging a production incident doesn't require the VPS container to
  still be running (Loki), and doesn't go dark if the NAS/tunnel is down
  either (local rotation).
- `docs/LOGGING.md` and `docs/OBSERVABILITY.md` exist and match what the
  codebase actually does, checkably in review.
