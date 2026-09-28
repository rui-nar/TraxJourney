# Logging

Conventions for adding or reviewing `logger.*()` calls anywhere in `api/` or
`src/`. This is the practical reference — if you're touching a route or
service file and need to know what level to use, how to format a message, or
what never gets logged, this page has it. For the *why* (issue #205's audit,
the infrastructure plan, open decisions) see
`docs/LOGGING_OBSERVABILITY_PLAN.md`; if the two ever disagree, the plan doc
is the source of truth and both should be updated together.

## Getting a logger

Always:

```python
from src.utils.logging import get_logger

_log = get_logger(__name__)
```

Never bare `logging.getLogger(__name__)`. Three files still do this
(`api/poster.py`, `src/email/service.py`, `src/tile_renderer.py`) — fix it if
you touch one of them, but don't go looking for others outside your change.

## Levels

| Level | Use for |
|---|---|
| `DEBUG` | Verbose internals; the access-log line for chatty/polled routes (see the allowlist below). |
| `INFO` | Normal lifecycle: a sync/import completed with counts, a job ran, a request completed (non-chatty route), an external call succeeded. |
| `WARNING` | Degraded-but-handled: retried, fell back, partial-batch failure, cache forced a refetch. |
| `ERROR` (via `.exception()`) | Something failed and the user got a worse outcome than they should have. |

For `ERROR`, always call `.exception()`, not `.error()`, inside an `except`
block — `.exception()` captures the traceback, `.error()` doesn't. This is
the single most common mistake to watch for in review.

## Format

`configure_logging()`'s formatter is logfmt-shaped, not free text: a logfmt
preamble (`request_id=... user_id=...`, populated by a `logging.Filter`)
followed by the human-readable message.

Why `request_id`/`user_id` live in the message body instead of becoming Loki
labels: Loki indexes labels, not message content, and a label whose values
come from user/request identity has unbounded cardinality — one Loki time
series per user or per request would eventually take Loki down with it (same
reasoning as `docs/METRICS.md`'s "no label may carry user data" rule for
Prometheus). So they're parsed out of the message body at query time instead,
via `| logfmt` in a LogQL query. Keep messages human-grep-able too — this
isn't JSON, it's logfmt.

Confirmed final format, as implemented in `configure_logging()`:

```
%(asctime)s - %(name)s - %(levelname)s - request_id=%(request_id)s user_id=%(user_id)s - %(message)s
```

Note `%(levelname)s` is positional, not a `level=` key — only `request_id`
and `user_id` are real logfmt tokens in the line today. A LogQL query
filtering by level needs a substring match (`|= "ERROR"`), not
`| logfmt | level=...` — see `docs/OBSERVABILITY.md`.

## Redaction

Never log:

- JWTs
- passwords
- Stripe secret keys / other API secrets
- E2EE key material
- raw request bodies
- a request's URL or query string — name a request by its **route template**
  (`route_template()` in `api/middleware.py`, what the exception handlers
  use); OAuth codes, share tokens and search terms travel in the URL
- a full client IP address — only the truncated form below

Email logging: log the recipient and subject only, never the body. Exception:
the `ConsoleEmailService` dev backend logging the full body is fine — that's
a dev-only code path, not something that reaches a shared log stream.

## Access log: client address and retention

The one line per request the middleware writes is

```
GET /api/projects/{name} -> 200 (12.3ms) ip=203.0.113.0/24
```

method, route template, status, duration, and the client address truncated
to its /24 (IPv6: its /48; an IPv4 client on a dual-stack socket is
truncated as IPv4). That is deliberately all of it (issue #443): no query
string, no path parameters, no full address. A request no route claimed —
a trailing-slash 307, a CORS preflight, a 404 — is named by its first
segment, plus the area segment under `/api`, the rest replaced by `...`
(`/api/share/...`, `/share/...`), never by its raw path; if a new URL
scheme ever puts a secret earlier than that, `_redacted_path()` in
`api/middleware.py` is where the rule lives. uvicorn's own access log, which
printed the full IP and the raw request line, is off (`--no-access-log` in
`entrypoint.sh`) — keep it off in any other way the server gets started.
The database engine runs with `hide_parameters=True` for the same reason:
a failed statement's exception text would otherwise carry its bound values
into the catch-all handler's `.exception()` line.

The truncated address exists for abuse investigation only (which network a
burst of failed logins or scraping came from); nothing rate-limits or
decides on it. Behind the reverse proxy it comes from `X-Forwarded-For`,
which uvicorn believes only from the peers `FORWARDED_ALLOW_IPS` lists
(`.env.example` explains the value to set in Docker and why it is a list of
networks rather than `*`; `entrypoint.sh` wires it). If every line shows
the same `172.x.x.0/24`, that variable is missing and you are logging the
Docker gateway, not clients.

Retention: lines ship to Loki and are kept **30 days**
(`docs/OBSERVABILITY.md`, "Loki retention"). The local Docker `json-file`
copy on the host is capped by size, not time (100 MB per service,
`docker-compose.yml.example`), which at current volume is shorter than
that. The privacy policy states what the access log holds and for how long
— change either here and update it there.

## External calls: `track_external()`

`src/utils/metrics.py:track_external()` is the single choke point for
logging (and metrics) around any outbound call to a third-party service. It
already wraps Strava, Polarsteps, Google Translate and SMTP for Prometheus
metrics, and is being upgraded to log too (`INFO` on success, `WARNING` on
failure) as part of issue #205.

If you're adding a new external HTTP call, wrap it in `track_external`
instead of hand-rolling your own try/except + log pattern:

```python
from src.utils.metrics import track_external

with track_external("polarsteps", "/v1/trips/{id}") as call:
    response = httpx.get(url)
    if response.status_code >= 400:
        call.outcome = outcome_for_status(response.status_code)
    response.raise_for_status()
```

Don't add a second, parallel logging pattern for external calls — if
`track_external` doesn't fit a case, that's worth raising rather than working
around.

## Chatty routes: DEBUG allowlist

Read-only, polled-or-per-paint routes log their access-log line at `DEBUG`
instead of `INFO`, so normal browsing doesn't drown out the signal:

- poster/job-status polling
- tile serving
- `GET /api/geo/project` (and its low-res variant)
- `GET /api/version`
- `/metrics`

Everything else — writes, auth, sync/import, external calls — logs at
`INFO`. This is a hardcoded path-template allowlist checked in **one place**,
the access-log middleware, not a per-route decorator. If you're adding a new
chatty/polled read-only route, add it to that allowlist rather than
suppressing its own logging locally.

## Testing

Every logging change needs a `caplog`-based test proving the *specific* log
line fires at the right level — not just that behavior is unchanged around
it. Add the test to the existing test file for that module if one exists;
otherwise create `tests/test_<module>_logging.py`.

```python
def test_malformed_activity_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        parse_activities_or_log([bad_activity], source="strava")
    assert "dropped" in caplog.text
    assert "strava" in caplog.text
```

## See also

- `docs/LOGGING_OBSERVABILITY_PLAN.md` — the full plan: audit of where
  logging is missing, infrastructure (Loki/Grafana/Alloy) decisions, open
  questions.
- `docs/METRICS.md` — Prometheus metrics; same cardinality discipline
  (`normalise_path`, closed label sets) that motivates keeping
  `request_id`/`user_id` out of Loki labels here.
