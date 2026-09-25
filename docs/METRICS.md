# Application metrics

Prometheus instrumentation for the FastAPI server (issue #125). Covers HTTP
traffic, authentication, third-party APIs, background jobs and the database.

## Enabling the endpoint

Set `METRICS_TOKEN` in the server's **runtime** environment (a compose
`environment:`/`env_file:` entry — not a `docker build` arg, which never
reaches the running process):

```bash
openssl rand -hex 32
```

Then `docker compose up -d` to recreate the container (a plain `restart` does
not apply env changes).

- **Unset** → `GET /metrics` returns **404**. The endpoint is off, and a probe
  can't tell it apart from a route that was never deployed.
- **Set** → `GET /metrics` requires `Authorization: Bearer <token>` and returns
  the Prometheus text exposition format. A wrong or missing header is a 401.

The token is checked on every request, so metrics can be switched on for a
running deployment without rebuilding the image.

Why a token at all: Caddy reverse-proxies *every* path of `traxjourney.com` to
the app (`docs/DEPLOYMENT_VPS.md`), so an unauthenticated `/metrics` would
publish user counts, route names, error rates and DB size to anyone who asks.

## Scraping

```yaml
scrape_configs:
  - job_name: traxjourney
    scheme: https
    static_configs:
      - targets: ["traxjourney.com"]
    authorization:
      credentials: "<METRICS_TOKEN>"   # or credentials_file: /etc/prometheus/traxjourney.token
```

Defence in depth — block the path at the proxy so only a local scraper (or an
SSH tunnel to `127.0.0.1:8000`) can reach it:

```
traxjourney.com {
    handle /metrics { respond 403 }
    reverse_proxy 127.0.0.1:8000
}
```

Quick check:

```bash
curl -s -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics | head
```

## What is collected

All metric objects live in one module, `src/utils/metrics.py`.

### HTTP — `prometheus-fastapi-instrumentator`

| Metric | Labels |
|---|---|
| `traxjourney_http_requests_total` | `method`, `handler`, `status` |
| `traxjourney_http_request_duration_seconds` | `method`, `handler` |
| `traxjourney_http_request_duration_highr_seconds` | — |
| `traxjourney_http_request_size_bytes`, `traxjourney_http_response_size_bytes` | `handler` |
| `traxjourney_http_requests_inprogress` | — |

`traxjourney_http_request_duration_highr_seconds` has no labels and many
buckets, for accurate overall percentiles; the labelled histogram keeps few.

`handler` is the **route template** (`/api/projects/{name}`), never the
concrete path. Status codes are deliberately not grouped into `2xx`/`4xx`:
409 (optimistic-lock conflict) and 401-vs-404 are individually actionable.
The in-progress gauge is what tells a slow request apart from a stuck one
(issue #45).

### Authentication

| Metric | Labels |
|---|---|
| `traxjourney_logins_total` | `provider` (`password`\|`google`), `result` (`success`\|`failure`) |
| `traxjourney_registrations_total` | `provider` |
| `traxjourney_app_opens_total` | `session_state` (`resumed`\|`login_required`) |

Google sign-in has no separate sign-up call, so `registrations_total{provider="google"}`
fires on first sight of an account and never again.

`traxjourney_logins_total` only counts a fresh credential submission — it misses
every launch where a cached session was simply resumed. `app_opens_total`
covers "returnability" instead: the client pings `POST /api/auth/app-opened`
once per launch, `resumed` when the cached session was still valid and
`login_required` when the user had to sign in from scratch.

### Third-party APIs

| Metric | Labels |
|---|---|
| `traxjourney_external_requests_total` | `service`, `endpoint`, `outcome` |
| `traxjourney_external_request_duration_seconds` | `service`, `endpoint` |

`service` ∈ `strava`, `polarsteps`, `google_translate`, `smtp`. `endpoint` is
templated (`/activities/{id}/streams`). `outcome` ∈ `success`, `client_error`,
`auth_error`, `rate_limited`, `server_error`, `exception`.

Strava counts **every retry attempt**, because each one spends real quota.

Strava's own quotas are tracked separately (issue #130):

| Metric | Labels |
|---|---|
| `traxjourney_strava_rate_limit_usage` | `window` (`15min`\|`daily`) |
| `traxjourney_strava_rate_limit_capacity` | `window` |
| `traxjourney_strava_throttled_total` | `window` |

`throttled_total` counts calls **our own** limiter refused before they reached
Strava — distinct from `outcome="rate_limited"`, which means Strava returned a
429. Usage is process-wide, which is the same single-worker assumption noted
under *Constraints*: a second worker would keep its own count and the app could
exceed the quota by a factor of the worker count.

### Background jobs

| Metric | Labels |
|---|---|
| `traxjourney_job_runs_total` | `job_name`, `result` (`success`\|`error`\|`missed`) |
| `traxjourney_job_duration_seconds` | `job_name` |
| `traxjourney_job_last_success_timestamp_seconds` | `job_name` |
| `traxjourney_prepared_geometry_backlog` | — |
| `traxjourney_prepared_geometry_outcomes_total` | `outcome` (`prepared`\|`unpreparable`\|`error`) |

Fed by a single APScheduler listener, so every job — `daily_backup`,
`wal_checkpoint`, anything added later — is covered automatically.

The label is `job_name`, not `job` (issue #435). The scrape attaches its own
`job` (and `instance`, and every label on the Alloy target, such as `env`), and
without `honor_labels` Prometheus keeps the scrape's value and renames the
app's to `exported_job`, so a `{job="daily_backup"}` filter matches nothing.
No app metric may use a label the scrape attaches;
`tests/test_dashboard_metrics_contract.py` enforces it, and also checks a
multiprocess-mode scrape (the production setup): every metric a dashboard
reads from the app's scrape job is exported (`process_*` included), no app
series carries a `pid` label, and every metric-to-metric ratio in a
dashboard divides series with the same labels. Series recorded before
the rename carry `exported_job` and do not join the `job_name` ones.

The prepared-geometry pair is set by the backfill sweep itself (issue #369),
not by that listener.
`prepared_geometry_backlog` counts activities still waiting for a prepared row,
including any that can never be prepared: it should fall to a small constant and
stay there. A flat non-zero line means the sweep runs and reports success
without making progress, which `job_runs_total` cannot show.

### Database

| Metric | Labels |
|---|---|
| `traxjourney_db_queries_total`, `traxjourney_db_query_duration_seconds` | `operation` (SQL keyword only) |
| `traxjourney_db_session_duration_seconds` | — |
| `traxjourney_db_errors_total` | `kind` (`pool_timeout`\|`locked`\|`operational`\|`other`) |
| `traxjourney_db_pool_connections` | `state` (`in_use`\|`idle`) |
| `traxjourney_db_pool_overflow`, `traxjourney_db_pool_capacity` | — |
| `traxjourney_db_file_size_bytes` | `file` (`main`\|`wal`) |
| `traxjourney_stale_writes_total` | — |

Pool and file-size gauges are computed at scrape time, so they cost nothing
between scrapes. The pool-utilisation panel divides `in_use` by capacity
`ignoring(state)`: without it the two sides never match and the panel is empty.

## Alerts worth having

| Symptom | Signal |
|---|---|
| Pool exhaustion — the issue #35 hang | `traxjourney_db_pool_connections{state="in_use"}` approaching `traxjourney_db_pool_capacity` (60), or any `traxjourney_db_errors_total{kind="pool_timeout"}` |
| WAL checkpointing has stopped | `traxjourney_db_file_size_bytes{file="wal"}` climbing without ever dropping — `wal_autocheckpoint=0` means only the `wal_checkpoint` job folds it back |
| Backup silently stopped | `time() - traxjourney_job_last_success_timestamp_seconds{job_name="daily_backup"} > 90000` |
| Strava quota nearly spent | `traxjourney_strava_rate_limit_usage / traxjourney_strava_rate_limit_capacity > 0.8` — imports start deferring past this |
| Strava quota actually hit | any `traxjourney_strava_throttled_total` (our limiter refused), or `traxjourney_external_requests_total{service="strava",outcome="rate_limited"}` (Strava refused) |
| Credential stuffing | `rate(traxjourney_logins_total{result="failure"}[5m])` |
| Write contention | `rate(traxjourney_stale_writes_total[5m])` |

## Constraints

- **Per-process registries.** Metrics live in the process' memory and
  `entrypoint.sh` runs one uvicorn worker, so a scrape sees everything that
  process served.
  It does **not** see another process. Two things put work outside the API
  process: a job `worker` container (`REDIS_URL` set — see issue #173), whose
  queued jobs record DB-session timings and `traxjourney_stale_writes_total` in
  their own registry; and multiple gunicorn workers, were that ever adopted.
  Both need `PROMETHEUS_MULTIPROC_DIR` pointing at a directory every process
  mounts — `/metrics` then aggregates the samples written there instead of
  reading its own registry. Unset, a scrape silently under-reports rather than
  failing, which is the trap worth knowing about. Set but empty counts as unset.
- **The multiprocess directory looks after itself** (issue #437,
  `src/utils/metrics_multiproc.py`). Nothing needs clearing by hand:
  - Files are named `<hostname>-<pid>`, not by PID alone. Every container has
    its own PID namespace, so PIDs repeat across containers: the RQ
    work-horses of `worker` and `worker-poster` get the same small PIDs, and
    two live processes on one file overwrite each other's samples. (The
    workers' parent processes are PID 1 like the API, but normally write no
    metrics: they only import the metrics module after a killed work-horse's
    handler has run.)
  - The directory is cleared by the first process that starts writing while
    no other writer is alive: on a stack start (`docker compose down && up`,
    which `deploy.ps1` does), or mid-run when the last writer has exited
    before the next one starts (the API restarting while no job runs, say).
    Every writer holds a shared `flock` on `.writers.lock` there, so a process
    starting beside a live one never deletes its files.
  - A container restarted or replaced on its own (`pull && up -d` without
    `down`) while others keep writing keeps the earlier files. Their counters
    stay in the totals, so `rate()` sees no false reset.
  - Between full stack stops, every RQ work-horse (one per job) leaves its
    counter, histogram and gauge files behind. That is by design: dropping
    them would make the totals go backwards. The file count therefore grows
    with the number of jobs run, and `/metrics` reads them all on every
    scrape. A periodic `docker compose down` then `up -d`, or any planned
    restart of the whole stack, clears them.
  - When an RQ work-horse exits, the worker calls prometheus_client's
    `mark_process_dead` for it, dropping its live-mode gauge files.
  - Gauges written from several processes pick how they combine, so a dead
    process never shows up as its own series: `job_last_success_timestamp_seconds`
    and `strava_rate_limit_capacity` take the `max`, and
    `prepared_geometry_backlog` the most recent value. None carries a `pid`
    label.
  - The pool, file-size and Strava-usage gauges are computed at scrape time by
    the process serving `/metrics`, the API (issue #455). They write no file,
    carry no `pid` label and hold the live value in both modes. The API's pool
    is the one requests queue behind; the Strava limiter lives in the API,
    the only process that calls Strava; the file size reads the same file from
    anywhere. Before #455 they were plain gauges, whose files held a 0 per
    process: production showed 0 on the pool and file-size panels, and the
    Strava quota alert could never fire.
  - Gauge files in a mode no current gauge uses are not read. An in-place
    upgrade (`pull && up -d`, no `down`) that changed a gauge's mode (as #437
    did, from `all`) leaves the old containers' files until the next clear;
    prometheus_client would otherwise take that gauge's mode from whichever
    file it read last. No one-off `down` is needed for this upgrade. A future
    change between two modes that are both still in use (say `max` to
    `mostrecent` while another gauge keeps `max`) is not covered: deploy that
    one with `docker compose down && up -d`.
  - `/metrics` also serves prometheus_client's process collector
    (`process_resident_memory_bytes` and the other `process_*` series), for
    the API process: the one the scrape job `traxjourney` names. It lives on
    the default registry, which multiprocess mode doesn't otherwise serve. The
    `python_*` platform and GC series are not served in this mode; no
    dashboard reads them.
  - `flock` needs a local filesystem seen by one kernel: a bind mount on the
    Docker host, not NFS or SMB.
- **Restarts reset counters.** That is normal — PromQL's `rate()`/`increase()`
  handle counter resets; only ever alert on rates, not absolute totals.
- **No label may carry user data.** Paths go through `normalise_path` and SQL
  through the operation keyword before being labelled. A label whose values
  come from user input creates one time series per distinct value.
- There is **no Prometheus/Grafana deployment yet** — this issue ships the
  endpoint only. Standing up the scraper is tracked separately.
