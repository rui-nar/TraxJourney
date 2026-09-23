# Observability: Loki + Prometheus + Grafana (issue #205)

Colocated on the NAS — generous disk, no CPU contention with the VPS's
production traffic. See `docs/DEPLOYMENT_VPS.md`'s "Observability" section
for the VPS-side half (the Tailscale tunnel + the Alloy shipper that pushes
here) and `docs/LOGGING.md` for the logging conventions the log lines below
follow.

## Why Loki + Prometheus, not Mimir

Grafana natively takes both Prometheus and Loki as datasources side by side
— that gets "everything in one Grafana" without introducing Mimir, which is
built for horizontal, multi-tenant scale that doesn't apply here (one app, a
handful of concurrent users). Plain Prometheus, with
`--web.enable-remote-write-receiver` so Alloy can push to it instead of
Prometheus reaching back out to scrape the VPS, is simpler and needs no
extra storage/ring component for zero benefit at this size. Revisit only if
this genuinely needs to scale multi-tenant later — repointing Alloy's
`remote_write` endpoint at Mimir afterwards is cheap; standing up Mimir now
for that hypothetical isn't.

## NAS-side compose (committed — `nas/`)

Unlike the VPS's real `docker-compose.yml` (host-specific, gitignored), the
NAS-side stack is checked into this repo under `nas/` — `nas/README.md` is
the step-by-step deployment runbook, `nas/docker-compose.yml.example` the
compose file. **Not verified against live Loki/Prometheus/Grafana/Tailscale
binaries from the session that wrote this** — treat it as a documented
starting point, the same caveat as the VPS-side Alloy config.

Loki, Prometheus and Grafana all run with `network_mode: service:tailscale`
— they share Tailscale's network namespace rather than publishing ports to
the NAS's LAN-facing bridge at all. This is stricter than "bind to the
Tailscale interface if your Docker setup allows it, otherwise rely on the
firewall," which was this section's original sketch — the sidecar pattern
removes the LAN-exposure question entirely instead of mitigating it. It
needs `/dev/net/tun` on the NAS; `nas/README.md` §0 covers checking for it
and the fallback (native Tailscale + firewall-reliant port binding) if your
NAS model doesn't have it.

## Loki retention

Cap it explicitly — "the NAS has disk to spare" is not the same as
"unbounded is fine." `nas/loki-config.yaml` defaults to 30 days, matching
the existing backup-retention convention (`src/backup/backup_service.py`);
revisit after seeing a few weeks of real volume.

## Grafana datasources and dashboards

`nas/grafana/provisioning/datasources/datasources.yaml`. Both point at
`localhost`, not a service name — the sidecar pattern above means Loki,
Prometheus and Grafana share one network namespace and reach each other
over loopback, not Docker's usual bridge-network service discovery.
Deliberately no fixed `uid` on either datasource — an earlier version
pinned one so dashboards could reference it deterministically, but that
made Grafana's provisioning module fail outright on every start
(confirmed live against `grafana/grafana:latest`, 13.1.3: `Datasource
provisioning error: data source not found`, crash-looping the container).
Each dashboard below instead has a `datasource`-type template variable
that resolves to whichever Prometheus/Loki datasource actually exists,
however it got its UID.

Five dashboards are provisioned from `nas/grafana/provisioning/dashboards/`
(a **TraxJourney** folder, read-only in the UI): **HTTP & Traffic**, **Jobs &
Database**, **Integrations & Auth**, **Logs**, and **Host Resources**. They
render the metrics table above and the LogQL queries below directly — see
`nas/README.md` §3 for where to find them once Grafana is up.

**Host Resources** is the odd one out: it's `node_*` metrics from Alloy's
`prometheus.exporter.unix` (VPS side, `config/alloy-config.river.example`),
not anything the app itself emits. Added after issue #209 — a VPS
livelocked under memory pressure with no swap configured, and nothing in
this stack was watching host memory at all, so the incident looked like
silence rather than a clean crash. Memory used %, swap used %, and "is swap
even configured" are the panels that would have shown that coming, alongside
CPU, load average and root-filesystem disk usage — all from collectors the
exporter already enables by default, so no Alloy config change was needed to
add them.

### Prod and val share this Grafana — the `env` variable

Prod and val run on the same physical VPS and both push to this same NAS
Loki/Prometheus (`docs/DEPLOYMENT_VPS.md` §5). Every series and log stream
Alloy ships carries an `env` label (`production` or `validation`), and every
one of the five dashboards has an `env` template variable (top-left) that
filters every panel on it — pick which environment you're looking at before
reading the numbers. `service="traxjourney"` is likewise a real label on
every log stream now, set unconditionally by Alloy's static relabel rule
(`config/alloy-config.river.example`) rather than assumed — the LogQL
queries below only started actually matching once that rule existed.

Each dashboard also has a `datasource` variable next to `env` — leave it on
its default. It exists so panels resolve the Prometheus/Loki datasource
dynamically instead of a fixed UID (see the datasources.yaml note above);
it isn't meant to be switched.

The `env` variable itself is populated via `label_values(<metric>, env)`,
and which metric that is matters: on a freshly-deployed or quiet instance,
a metric that only appears once real traffic occurs (`traxjourney_http_requests_total`,
`traxjourney_logins_total`, ...) means the `env` dropdown comes up empty and the
whole dashboard looks broken even though the pipeline is fine — confirmed
live on a real val deployment with zero traffic yet. HTTP & Traffic, Jobs &
Database and Integrations & Auth all key off `up{job="traxjourney"}` instead —
a synthetic series Prometheus/Alloy create for every successful scrape,
independent of anything the app itself has done. Host Resources already
had this right by construction (`node_memory_MemTotal_bytes`, from
node_exporter, not the app).

## Queries an operator actually runs

Log lines look like:

```
2026-08-10 18:06:58,123 - api.polarsteps - WARNING - request_id=a1b2c3d4 user_id=42 - polarsteps connect failed user_id=42
```

`request_id=` and `user_id=` are real logfmt tokens — `| logfmt` extracts
them as labels for the query. `%(levelname)s` (`WARNING` above) is
**positional, not a `level=` key** (see `docs/LOGGING.md`), so level
filtering is a substring match, not a logfmt field.

- **One request end to end** — the original "reconstruct what happened,
  without asking the user" ask, scoped to a single request:
  ```logql
  {service="traxjourney"} | logfmt | request_id="a1b2c3d4"
  ```
- **One user's whole session across concurrent traffic** — the broader
  version of the same ask, across every request that user made in a time
  window:
  ```logql
  {service="traxjourney"} | logfmt | user_id="42"
  ```
- **Error rate** (substring match — see the level-field note above):
  ```logql
  sum(rate({service="traxjourney"} |= "ERROR" [5m]))
  ```
- **A specific external integration's failures** (matches
  `track_external()`'s log format, `src/utils/metrics.py`):
  ```logql
  {service="traxjourney"} |= "external call failed" |= "service=polarsteps"
  ```

## Alerting (replaces the earlier idea of adding Sentry)

Self-hosted, no third-party data egress — a better fit for a product built
around zero-knowledge E2EE than shipping stack traces to an external
service (`docs/ENCRYPTION.md`). Two starting rules in Grafana Alerting:

1. **Error-rate spike** — the LogQL query above, alert if `> N` errors over
   5 minutes for some threshold `N` worth calibrating against real traffic
   first (start loose, tighten once you know the baseline).
2. **Scheduled job failure** — `traxjourney_job_runs_total{result="error"}`
   (Prometheus, already emitted by `src/utils/metrics.py`'s
   `record_job_event`) — alert on any increment, since a failed daily
   backup or WAL checkpoint is always worth knowing about immediately, not
   discovering days later.

## Dozzle

Already deployed (`/opt/dozzle` on the VPS, port 8892) and keeps covering
live-tail — not part of this stack, and this doesn't replace it. Loki adds
"filter by user/request after the fact, across concurrent traffic, after a
deploy has recycled the container" — which Dozzle was never meant to do.
Worth a one-time check that it isn't reachable without auth on a
publicly-routed port (Dozzle has none built in).
