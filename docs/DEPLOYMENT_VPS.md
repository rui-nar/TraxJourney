# VPS Deployment (traxjourney.com)

Runbook for the OVH VPS, which now hosts **both** environments and has fully
replaced the Synology NAS:

- **production** — `traxjourney.com`, `/opt/traxjourney/`, image `:latest`
- **validation** — `val.traxjourney.com`, `/opt/traxjourney-val/`, image `:validation`

A host still laid out under the app's old name (`/opt/viewtrip`, service
`viewtripweb`, `viewtripweb.db`) is moved to these names with
`docs/RENAME_TRAXJOURNEY_RUNBOOK.md` (issue #151).

Validation moved off the NAS because its firewall could not be opened up enough
to serve the environment. Nothing about TraxJourney forced the move, so there is
no application-level workaround to look for — the NAS is simply no longer a
deployment target, and `deploy.ps1` no longer has a code path that reaches it.

## Infrastructure

| Item | Value |
|------|-------|
| Domain | traxjourney.com (+ www, redirected to apex), val.traxjourney.com |
| Provider | OVH, VPS-1 2027 range |
| Specs | 2 vCore, 4 GB RAM, 40 GB NVMe SSD, Strasbourg (FR) datacenter |
| Image | Debian 12 - Docker (Docker preinstalled) |
| SSH user | `debian` (OVH default account, sudo + docker group) |
| Prod directory | `/opt/traxjourney/` (`db/`, `config/`, `data/`, `docker-compose.yml`, `.env`) |
| Val directory | `/opt/traxjourney-val/` (same layout, own `.env`, own data) |
| Reverse proxy | Caddy (automatic Let's Encrypt TLS) |

## 1. VPS hardening

- SSH key auth only: generated a dedicated key locally
  (`~/.ssh/traxjourney_vps`), copied to `~/.ssh/authorized_keys` for the
  `debian` user, then in `/etc/ssh/sshd_config`:
  - `PermitRootLogin no` (already default on the OVH image)
  - `PasswordAuthentication no`
  - `systemctl restart sshd` — **always verify the key login works from a
    fresh terminal before this step**, to avoid a lockout.
- Firewall: `ufw` (not raw `iptables` — Debian doesn't ship ufw by default,
  `apt install ufw` first). Rules: allow OpenSSH, 80/tcp, 443/tcp, then
  `ufw enable`.
- GHCR auth: logged in with a **read-only** PAT (`read:packages` scope only),
  not the push token used by `deploy.ps1` locally — limits blast radius if
  the VPS is ever compromised.

**Gotcha:** Docker manipulates `iptables` directly when a container port is
published (`ports:` in compose), and can bypass ufw's rules entirely. Fix:
bind published ports to loopback only, e.g. `"127.0.0.1:8000:8000"`, so the
port is reachable from Caddy on the same host but never exposed publicly
regardless of firewall state.

## 2. Caddy reverse proxy

Installed from Caddy's official apt repo. `/etc/caddy/Caddyfile`:

```
traxjourney.com {
    reverse_proxy 127.0.0.1:8000
}

www.traxjourney.com {
    redir https://traxjourney.com{uri} permanent
}

val.traxjourney.com {
    reverse_proxy 127.0.0.1:8001
}
```

Each hostname gets its own certificate automatically. No `tls` directive is
needed, and WebSocket upgrades pass through without extra configuration.

**Gotcha:** the Caddy Debian package ships a default `:80 { root * ...;
file_server }` block in the Caddyfile. This block has no hostname, so it
matches *any* host on port 80 and will intercept requests meant for your
domain block — including the ACME challenge for a newly added subdomain.
Delete it, don't just append your own block alongside it.

**DNS before reload.** A new hostname needs its A record live *before* the
first request, or the HTTP-01 challenge fails and Caddy backs off with a retry
timer. `dig` is not installed on the OVH Debian image (it is in `dnsutils`);
`getent hosts val.traxjourney.com` answers the same question with nothing to
install. If that comes back empty but the record is definitely published,
query a public resolver directly — `getent` goes through the local resolver
and can serve a stale negative answer:

```bash
curl -s "https://dns.google/resolve?name=val.traxjourney.com&type=A"
```

`caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy` after
any edit. Cert issuance is automatic on first request, provided DNS already
resolves and 80/443 are open.

**Keep-alive: Caddy must be the side that closes idle upstream connections.**
Caddy pools its connections to uvicorn and keeps an idle one for 2 minutes by
default (`transport http { keepalive }`). `entrypoint.sh` therefore runs uvicorn
with `--timeout-keep-alive 300`; its 5 s default made uvicorn close first, and a
request that Caddy sent on a pooled connection at that same instant failed
with a 502 (`journalctl -u caddy`: `"msg":"EOF"` or `read: connection reset by
peer`, on POST/PUT/DELETE — Go's HTTP client retries GETs by itself). If you
ever set `keepalive` explicitly in the Caddyfile, keep it below uvicorn's value;
`tests/test_entrypoint_keepalive.py` pins the entrypoint side, and
`docs/repro/keepalive_502/` reproduces the race (issue #400).

## 3. App deployment

`/opt/traxjourney/docker-compose.yml` mirrors the NAS prod compose
(`/volume2/docker/viewtrip/docker-compose.yml`), with three differences:

- Port bound to loopback (`127.0.0.1:8000:8000`) instead of `7777:8000`.
- `FRONTEND_ORIGIN` / `STRAVA_REDIRECT_URI` point at `traxjourney.com`.
- `JWT_SECRET` freshly generated (`openssl rand -hex 32`) — not reused from
  the NAS, no reason to carry an old session-signing key to a new host.

All other env values (Mapbox, Google client ID/Translate key, Strava
client ID/secret) are unchanged from NAS prod.

**Before going live** (done for prod, and the same applies to any new
hostname — see §5): add the `https://traxjourney.com/...` equivalents to
Google OAuth console's authorized redirect URIs and Strava's app
"Authorization Callback Domain". Login and Strava sync fail on a domain that
is not registered there.

### Background job worker (optional, issue #173)

The API serves fine on its own: with `REDIS_URL` unset, every background job
(route resolution, poster rendering, share tiles, stats) runs inside the API
process via FastAPI `BackgroundTasks`. That is the original behaviour and a
reasonable setup for this instance's load.

**For the poster feature specifically, treat this as strongly recommended,
not merely optional.** An A0 render is CPU-heavy (Pillow compositing up to
~140M pixels) and does blocking sequential Mapbox tile fetches; run without
`REDIS_URL` and that work executes inside the API process itself, so it
starves every other request on the host for the duration of the render —
not just the poster job's own caller. `enqueue()` logs an `_log.error(...)`
each time this fallback is used for the poster queue, so check the API logs
if posters feel like they're stalling the rest of the app.

Adding the `redis` + worker services buys two things:

- **Durability.** A job survives an API restart. Previously a restart mid-resolve
  left a segment on `route_status="pending"` forever, and the only recovery was
  client-side — it ran when someone happened to reopen the project.
- **A real concurrency bound.** An RQ worker runs one job at a time, so the
  number of processes listening on a queue *is* that queue's parallelism.

The bounds are not the same for every queue, which is why there are two worker
services rather than one scaled to two replicas (issue #188): `resolve` is
capped at 2 because Overpass rate-limits per IP and it is a free public service,
while `poster` is capped at **1** because two concurrent A0 renders are what
takes a small host out on memory. Identical replicas can only give every queue
the same bound, so they cannot express this.

`QUEUE_MAX_CONCURRENCY` in `src/jobs/queue.py` is the source of truth for those
numbers, and `tests/test_worker_topology.py` fails if the compose example stops
matching it. Change the map and the compose file together.

See `docker-compose.yml.example` for the service definitions. Three things about
it are easy to get wrong:

- The workers **must mount the same host paths** as the API container. The
  database is a SQLite file, so "sharing" it means sharing the volume, on one
  host. Two engines writing one file is fine — WAL and a 30 s `busy_timeout` are
  set on every connection (`models/db.py`) — but only on the same machine.
- Redis needs `--appendonly yes`. Without it, a broker restart silently drops
  every queued job, which is the exact failure the queue was added to fix.
- Do **not** publish Redis' port. Docker publishes ports by writing DNAT rules
  that bypass `ufw`, so "the firewall blocks it" is not true of a published
  port — and an unauthenticated Redis reachable from the internet is a
  well-known way to lose a host.

The workers set `TRAXJOURNEY_ROLE=worker`, which stops them running migrations, the
admin seed, and the scheduled jobs. Only the API container owns those: two
containers racing `alembic upgrade head` at boot, or each taking its own nightly
backup and 60 s WAL checkpoint, is the failure that guards against.

Keep both worker services on the **same image tag** as the API. They share one
image and only the API runs migrations, so a worker left on an older tag would
run stale job code against a schema it does not know about.

`deploy.ps1` needs no changes for any of this: it builds/pushes the image and
then runs `docker compose down && pull && up -d`, which is service-agnostic.
Adding the services to each host's compose file and the keys to its `.env` is
the whole deployment change.

**Rollback** is unsetting `REDIS_URL` and removing the worker services. No image
rebuild — jobs simply run in-process again.

If you scrape `/metrics`, also set `PROMETHEUS_MULTIPROC_DIR` to a directory
both containers mount. Without it the scrape only sees the API process and
job-side DB metrics silently go missing (see docs/METRICS.md).

## 4. Data migration (NAS -> VPS)

*Historical — the one-time move of prod data off the NAS. Kept for the gotchas,
which apply to any Synology copy. To seed validation from prod, see §5. The
paths are the ones used at the time, before the rename (issue #151): the VPS
directory is now `/opt/traxjourney` and the database `traxjourney.db`.*

1. **Consistent DB snapshot on the NAS**, via SQLite's online backup API in a
   throwaway container (safe on a live DB — unlike a raw `cp`, correctly
   handles WAL mode, which the app already runs — see `models/db.py`):
   ```bash
   ssh -p <nas-ssh-port> <nas-user>@<nas-host> "DOCKER=\$(command -v docker 2>/dev/null || ls /var/packages/ContainerManager/target/usr/bin/docker /var/packages/Docker/target/usr/bin/docker /usr/local/bin/docker 2>/dev/null | head -1); \$DOCKER run --rm -v /volume2/docker/viewtrip/db:/db python:3.11-slim python3 -c \"import sqlite3; s=sqlite3.connect('/db/viewtripweb.db'); d=sqlite3.connect('/db/migration_backup.db'); s.backup(d); d.close(); s.close(); print('backup done')\""
   ```
   **Gotcha:** `docker` isn't in `PATH` for non-interactive SSH sessions on
   Synology — same issue `deploy.ps1`'s remote script already works around;
   resolve the binary path explicitly as above.

2. **Stop the VPS container first** if anything is already running there —
   overwriting a SQLite file out from under a live connection risks
   corruption: `cd /opt/viewtrip && docker compose down`.

3. **Copy DB + data + config from NAS straight to the VPS** (no need to hop
   through a local machine — the VPS has direct SSH reach to the NAS):
   ```bash
   scp -O -P <nas-ssh-port> <nas-user>@<nas-host>:/volume2/docker/viewtrip/db/migration_backup.db /opt/viewtrip/db/viewtripweb.db
   rsync -avz -e "ssh -p <nas-ssh-port>" <nas-user>@<nas-host>:/volume2/docker/viewtrip/data/ /opt/viewtrip/data/
   rsync -avz -e "ssh -p <nas-ssh-port>" <nas-user>@<nas-host>:/volume2/docker/viewtrip/config/ /opt/viewtrip/config/
   ```
   **Gotchas encountered:**
   - `scp` alone failed with `subsystem request failed` — modern OpenSSH
     clients default to SFTP-based scp, which Synology's sshd doesn't have
     enabled. Fix: `-O` forces the legacy SCP protocol.
   - Legacy `-O` scp doesn't support the `source_dir/.` trick for copying a
     directory's contents without the directory itself (`unexpected
     filename: .`) — use `rsync` for directories instead.
   - `rsync` wasn't runnable on the NAS until DSM's **Control Panel > File
     Services > rsync** service was explicitly enabled (installs/activates
     the `rsync` binary as a side effect, even though the connection here
     tunnels through SSH rather than using the native daemon protocol).
   - Ensure destination ownership is correct first (`sudo chown -R
     $USER:$USER /opt/viewtrip`) — Docker can leave bind-mount dirs owned by
     root from an earlier `up`.

4. `cd /opt/viewtrip && docker compose up -d`, then `docker compose logs -f`
   to confirm a clean Alembic migration + startup before checking the site.

## 5. Validation environment (`/opt/traxjourney-val`)

Same host, same Docker, same image repository — a second directory with its own
compose file, `.env` and data. Three things differ from prod:

- **Port `127.0.0.1:8001:8000`.** Loopback-bound for the reason in §1: Docker
  publishes ports by writing DNAT rules that bypass `ufw`, so a bare
  `8001:8000` would put validation on the public internet regardless of the
  firewall. Only Caddy needs to reach it.
- **Image `:validation`**, not `:latest`.
- **Its own `db/`, `data/`, `config/`.** Pointing val at `/opt/traxjourney/db`
  would put validation writes into the production SQLite file.

`.env` must set `FRONTEND_ORIGIN` and `STRAVA_REDIRECT_URI` to
`val.traxjourney.com`, or CORS rejects the client and Strava's callback lands
on prod. Add `val.traxjourney.com` to Google OAuth's authorized redirect URIs;
Strava allows only one callback domain per app, so val either gets its own
Strava app or does without Strava sync.

If the `alloy` service (§7) is running, `.env` must also set
`TRAXJOURNEY_ENV=validation` here (prod's is `TRAXJOURNEY_ENV=production`) — both
push to the same NAS Loki/Prometheus, and this is the only thing that keeps
their logs/metrics distinguishable there instead of colliding. Each
dashboard's `env` variable filters on it.

**Use Stripe test keys and val's own webhook secret.** This matters more than it
looks once the section below is used: a database seeded from prod carries real
`stripe_customer_id` values, and val running unreleased code against live Stripe
keys can bill or mutate a real customer.

### Seeding val from the latest prod backup

Prod's nightly job writes `/opt/traxjourney/db/backups/traxjourney_<YYYY-MM-DD>.db`
(`src/backup/backup_service.py`). Each file is self-contained —
`wal_checkpoint(TRUNCATE)` folds the WAL in — so there is no sidecar to carry
along. Both stacks are on one host, so a bind mount reaches them:

```yaml
  # One-shot: seed val's DB from prod's newest nightly backup.
  # Behind a profile so a routine `docker compose up -d` can never clobber
  # val — see "Run it" below for the commands.
  db-seed:
    image: alpine:3.20
    profiles: ["seed"]
    volumes:
      - /opt/traxjourney/db/backups:/prod-backups:ro
      - ./db:/db
    command:
      - sh
      - -euc
      - |
        latest=$$(ls -1 /prod-backups/traxjourney_*.db 2>/dev/null | tail -n1)
        [ -n "$$latest" ] || { echo "no prod backup in /prod-backups"; exit 1; }
        echo "seeding val from $$latest"
        rm -f /db/traxjourney.db-wal /db/traxjourney.db-shm
        cp "$$latest" /db/traxjourney.db
        echo done
```

**Run it** (from `/opt/traxjourney-val`, in this order — val must be stopped
before the copy, per the first gotcha below):

```bash
docker compose down
docker compose --profile seed run --rm db-seed
docker compose up -d
```

The `--profile seed` flag is required on that middle command — `db-seed` is
in the `seed` profile specifically so it never runs as a side effect of a
plain `docker compose up -d` (see the third gotcha below). `--rm` cleans up
the one-shot container after it exits; it isn't a long-running service.

Three things about it are easy to get wrong:

- **Deleting the `-wal` / `-shm` sidecars is not optional.** If val has been
  running, `./db` holds a WAL belonging to *val's* database. Drop a different
  `.db` next to it and SQLite replays that WAL onto the new file — a corrupt
  database, not a clean copy.
- **Run it with val stopped** (`docker compose down` first). Overwriting a
  SQLite file under a live connection is the same hazard as §4 step 2.
- **The profile is what keeps it safe.** Wired as a `depends_on:
  service_completed_successfully` dependency instead, every `docker compose
  up -d` would silently reset val, which makes it useless for testing anything
  that spans a deploy.

Backups are ISO-dated, so `ls -1 | tail -n1` really is the newest. `$$` is
compose escaping — the shell receives a single `$`.

Schema drift needs no action: val's API runs `alembic upgrade head` in its
lifespan, so a prod DB on an older revision migrates forward on first boot.

The database references media under `data/`, so a DB-only seed leaves memories
pointing at files that do not exist. Add `/opt/traxjourney/data:/prod-data:ro` plus
`./data:/val-data` and `cp -a /prod-data/. /val-data/` if you want them — check
free space first, it is a full copy of prod's media onto a 40 GB disk.

## 6. `deploy.ps1`

`-Target Validation|Prod` (default `Validation`). Both targets SSH to the VPS
(the host, user and key from your local deploy configuration) and run
`docker compose down / pull / up -d`; they differ in directory, image tag and
whether anything is built locally.

| | `Validation` | `Prod` |
|---|---|---|
| Directory | `/opt/traxjourney-val` | `/opt/traxjourney` |
| Image tag | `:validation` | `:latest` |
| Builds locally | yes (unless `-SkipBuild`) | never |
| URL | val.traxjourney.com | traxjourney.com |

```powershell
.\deploy.ps1                      # build working tree -> :validation -> val
.\deploy.ps1 -FromMain            # build a clean export of origin/main instead
.\deploy.ps1 -SkipBuild           # deploy the :validation CI already published
.\deploy.ps1 -Target Prod         # pull :latest, no build
```

`-FromMain` builds a pristine export of `origin/main` in a throwaway git
worktree, so the image is exactly what is on main — never contaminated by local
edits or untracked files.

Every path that skips the build first checks GitHub Actions for an in-progress
`docker-build.yml` run and refuses to deploy while one is going, since the tag
it is about to pull may be stale or only half-pushed.

Note `deploy.ps1` itself is **gitignored** and lives only on the dev machine.

### The other way to cut `:validation`

A session with no local Docker — a web Claude Code session, say — produces the
same image by force-pushing the floating `validation` git tag:

```bash
git tag -f validation <commit-or-branch>
git push origin validation --force
```

`docker-build.yml` builds `ghcr.io/rui-nar/traxjourney:validation` on
`ubuntu-latest`. It is the **same tag** `deploy.ps1` pushes, so the val host
pulls it either way and needs no reconfiguration. Deploy it with
`.\deploy.ps1 -SkipBuild`, or directly on the VPS:

```bash
cd /opt/traxjourney-val && docker compose pull && docker compose up -d
```

This path only ever produces `:validation` — never `:latest` or a `:<sha>` tag,
which stay reserved for real `v*` releases.

Building on Linux is also the only way to avoid issue #190: `deploy.ps1` builds
from a Windows working tree, which is how CRLF line endings on a `*.sh` file
baked a broken `#!/bin/sh\r` shebang into the image and crash-looped the worker
containers. `.gitattributes` pins shell scripts to LF, but only on a fresh
checkout of the affected path, not retroactively.

**One consequence of the shared tag:** two producers write `:validation`, and
the host cannot tell which one it is running. If a local build and a tag push
race, last writer wins. Prefer the tag route when it matters who built it.

## 7. Observability: Loki/Prometheus/Grafana on the NAS (issue #205)

Logs and metrics ship off this VPS to a stack colocated on the NAS —
generous disk there, no CPU contention with production traffic here. See
`docs/OBSERVABILITY.md` for the NAS-side compose, retention, and the
LogQL/PromQL an operator actually runs. This section is only the VPS-side
half: the tunnel and the shipper.

### Tailscale tunnel

Deliberately not a port-forward + DDNS + bearer token, the way `/metrics`
is secured today (`docs/METRICS.md`) — the NAS has never had an inbound
port opened for anything but SSH, and a second internet-facing ingestion
endpoint there is a materially different risk than this VPS's already
locked-down setup (§1). Install Tailscale on both hosts instead, so
Loki/Prometheus never touch the public internet at all:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Same on the NAS — DSM's Package Center or Container Manager, depending on
DSM version. `tailscale status` on either host shows the other's Tailscale
IP/MagicDNS name — that's what `LOKI_PUSH_URL` and
`PROMETHEUS_REMOTE_WRITE_URL` below point at.

### Alloy (VPS side)

`docker-compose.yml.example`'s `alloy` service tails every container's logs
via the Docker socket (read-only), scrapes `traxjourney:8000/metrics` over
the compose-internal network — `/metrics` itself never needs to be reachable
from outside this host for this to work — and (issue #209) scrapes its own
`prometheus.exporter.unix` component for host memory/swap/CPU/disk. That
component reads the host's real `/proc`/`/sys`/`/`, mounted read-only into
the container (`/host/proc`, `/host/sys`, `/host/root` in the `alloy`
service's `volumes:`) — without those mounts it still runs, it just reports
the container's own namespaced view instead of the host's, which for memory
in particular is not what you want after issue #209. Copy the example config
and fill in `.env`:

```bash
cp config/alloy-config.river.example config/alloy-config.river
```

```
LOKI_PUSH_URL=http://<nas-tailscale-host>:3100/loki/api/v1/push
PROMETHEUS_REMOTE_WRITE_URL=http://<nas-tailscale-host>:9090/api/v1/write
```

**Not verified against a live Alloy binary** — `config/alloy-config.river.example`
is a documented starting point to adapt, the same spirit
`docker-compose.yml.example` itself already is. Confirm log lines, the
`traxjourney_*` metrics, and the `node_*` host metrics actually arrive in
Grafana on the NAS (`TraxJourney / Host Resources` dashboard) before relying
on it for an incident.

### Dropping `/metrics`'s public exposure

Once Alloy's scrape is confirmed working, `/metrics` no longer needs the
bearer-token + Caddy-block setup in `docs/METRICS.md` — Alloy reaches it
over the internal compose network, never the public internet. Remove the
`handle /metrics { respond 403 }` Caddy block (§2) and unset
`METRICS_TOKEN`, or leave the token as defence in depth and just drop the
Caddy exposure — either is fine, but the token alone was always the weaker
of the two.

### Keep local rotation regardless

Every service in `docker-compose.yml.example` now sets `max-size`/`max-file`
on its `logging:` driver (previously unbounded — a latent disk-fill risk on
this 40GB host). This stays even with Loki live: if the NAS or the tunnel
is down during an incident, `docker compose logs` here must still answer
"what just happened" on its own.

## 8. Auto-deploy validation on new image (issue #205)

`deploy.ps1` needs a human at a Windows dev machine to redeploy validation.
`vps/webhook/` closes that loop: `docker-build.yml` finishing successfully
off the `validation` tag triggers a `docker compose pull && up -d` on
`/opt/traxjourney-val` automatically, with no runner registered in GitHub and
no SSH key stored in GitHub's secrets — the trust boundary stays entirely
on this VPS. Deliberately scoped to **validation only**: auto-deploying
prod on every release would remove `deploy.ps1 -Target Prod`'s existing
manual gate, which is a bigger safety call than "keep val fresh" and not
something to fold in as a side effect of this.

**Not verified against a live `webhook` binary from the session that wrote
this** — `vps/webhook/` is a documented starting point, same caveat as the
Alloy/Loki configs above.

### Install `webhook`

```bash
sudo apt install webhook   # Debian's own repo; check `webhook -version` after
```

If it's missing or too old there, grab a static binary from
[adnanh/webhook's releases](https://github.com/adnanh/webhook/releases)
instead and drop it at `/usr/bin/webhook`.

### Configure the hook

```bash
mkdir -p /opt/traxjourney-val/webhook
cp vps/webhook/*.sh vps/webhook/hooks.yaml.example /opt/traxjourney-val/webhook/
cd /opt/traxjourney-val/webhook
mv hooks.yaml.example hooks.yaml
openssl rand -hex 32   # generate a secret, paste it into hooks.yaml AND
                        # into GitHub's webhook config below — same value
```

`hooks.yaml` is gitignored (the secret lives inline — `webhook` has no
env-var interpolation in its config), same pattern as `docker-compose.yml`
and `config/config.json` elsewhere in this repo.

### systemd unit

```bash
sudo cp /opt/traxjourney-val/webhook/webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now webhook
sudo systemctl status webhook   # confirm it's listening on 127.0.0.1:9999
```

### Caddy routing

Loopback-bound (`127.0.0.1:9999`), same discipline as everything else in
§1 — add a `handle_path` block to the existing `traxjourney.com` site
(order matters: this must come before the catch-all `reverse_proxy
127.0.0.1:8000`, same as the existing `/metrics` block):

```
traxjourney.com {
    handle /metrics { respond 403 }
    handle_path /gh-webhook/* {
        reverse_proxy 127.0.0.1:9999
    }
    reverse_proxy 127.0.0.1:8000
}
```

`handle_path` (not `handle`) strips the `/gh-webhook` prefix before
forwarding — `webhook`'s own server serves each hook at `/hooks/<id>`
relative to its own root (its default `-urlprefix`), so the public URL
ends up `https://traxjourney.com/gh-webhook/hooks/deploy-validation`.
`caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`
after editing, per §2.

### GitHub-side webhook

Repo → Settings → Webhooks → Add webhook:

| Field | Value |
|---|---|
| Payload URL | `https://traxjourney.com/gh-webhook/hooks/deploy-validation` |
| Content type | `application/json` |
| Secret | same value as `hooks.yaml`'s `secret` |
| Events | "Let me select individual events" → **Workflow runs** only |
| Active | checked |

No changes needed to `docker-build.yml` itself — repo webhooks subscribe to
workflow-run completions independently of the workflow's own
`permissions:` block.

### Verify

Force-push the `validation` tag (see §6's "other way to cut `:validation`")
and watch:

```bash
tail -f /opt/traxjourney-val/deploy.log
```

`deploy-validation.sh` logs each attempt (triggered/succeeded/failed) with
a UTC timestamp, and is `flock`-guarded so a retried GitHub delivery for
the same build can't run a second `pull`/`up -d` concurrently against the
same compose project.

## 9. Rail data (issue #345)

Train route geometry is read either from Overpass (`RAIL_SOURCE` unset or
`overpass`) or from per-region SQLite stores on this box (`RAIL_SOURCE=local`).
The stores are not in the image and nothing fetches them at boot: they are a
deployment step, run here, against the `rail-data-<date>` prereleases the
`Rail extract` workflow publishes on GitHub.

Everything below is `docker compose run --rm` in the **prod** stack
(`/opt/traxjourney`); val is the same with `-f` pointed at `/opt/traxjourney-val`.
Starting a second refresh of the same directory is harmless: it takes an
exclusive lock on `.incoming/.lock`, so the second run prints that one is
already going, does nothing and exits 0.

### First install

```bash
cd /opt/traxjourney
docker compose run --rm --entrypoint python traxjourney \
    scripts/fetch_rail_data.py --dest /app/data/rail
```

`./data` is already a bind mount shared by the API and both workers, so
`/app/data/rail` is the same directory for all of them and survives
`up -d`, image pulls and reboots.

That takes a while the first time — 49 European countries, each one downloaded
(single-digit MB) and built into a store (Germany, the largest, ~12 s). Expect
a few hundred MB and up to ~1 GB in `./data/rail` when it finishes; check
`df -h` first, the host has 40 GB for both stacks. Only one country's extract
and its store exist at a time, and the extract is deleted the moment the store
is built, so the peak is bounded whatever the coverage grows to.

Then, and only then, turn it on:

```bash
echo 'RAIL_SOURCE=local'            >> .env
echo 'RAIL_DATA_DIR=/app/data/rail' >> .env
docker compose up -d        # `restart` does NOT pick up .env changes
```

Setting the flag with no data is not an outage but it is pointless: every
resolve falls back to Overpass, which is the traffic this exists to avoid.

**This one time it needs the image to have been rebuilt since issue #345's
delivery step landed.** The builder needs `libexpat1`, which the Dockerfile now
installs and older images do not have — `docker compose pull` first if
`import osmium` fails. Data refreshes after that need no image at all.

### Refresh

The same command. It re-reads the newest release, skips every region whose
store it already holds at that release's checksum, and fetches only what
changed:

```bash
docker compose run --rm --entrypoint python traxjourney \
    scripts/fetch_rail_data.py --dest /app/data/rail
```

Monthly is ample — rail alignments change over years, and the workflow rebuilds
on the 2nd of each month. There is deliberately no timer installed: scheduling
and data-age alerting are Phase 5 of `docs/LOCAL_RAIL_DATA_PLAN.md`.

**A release that bumps the store schema also needs one of these runs**, and the
step notices on its own: the sidecar beside each store records the schema it
was built at as well as the asset digest, so a bump rebuilds every region even
though the published extract has not changed. Expect `49 installed, 0 up to
date` rather than the usual near-total skip, and roughly the time of a first
install. It is not urgent and there is no window to plan around — the reader
accepts the previous schema as well as the current one, so the box keeps
serving the stores it already has until each is replaced. Issue #359 is the
first such bump: schema 1 → 2, adding member roles and stop sequences.

**No restart is needed and none is wanted.** Every file is built elsewhere and
moved into place with an atomic rename, so a worker mid-resolve keeps reading
the file it opened, whole, while the new one takes its name. Each process
rebuilds its view of the directory at most every five minutes
(`_LOCAL_SOURCE_TTL_S`), so new data is live within that window.

### What to check

The step exits non-zero if any region was refused, and names them. Otherwise:

```bash
# 1. Every ok region in the manifest has a store beside it.
ls /opt/traxjourney/data/rail/*.rail.sqlite | wc -l
python3 -c "import json;m=json.load(open('/opt/traxjourney/data/rail/manifest.json'));\
print(sum(1 for e in m['regions'] if e['status']=='ok'), 'ok', \
      sum(1 for e in m['regions'] if e['status']=='empty'), 'empty')"

# 2. How old the data is — per region, which is the number that matters.
python3 -c "import json;m=json.load(open('/opt/traxjourney/data/rail/manifest.json'));\
print(sorted({e.get('source_date') for e in m['regions']}))"

# 3. Nothing left mid-flight.
ls -A /opt/traxjourney/data/rail/.incoming   # must hold nothing but .lock

# 4. Resolves are actually using it: no Overpass traffic for European
#    segments, and no "local rail data ... is unusable" in the logs.
docker compose logs --since 10m traxjourney worker | grep -i "rail"
```

A count of ok regions that is lower than the manifest's is the failure that
looks like success: those countries silently fall back to Overpass. Re-run the
step; it retries only them.

`docker compose run` inherits the API service's memory limit (768 MB), and
`build_store` peaks around 32 MB on Luxembourg — extrapolating to ~230 MB for
Germany, so the headroom is real but not enormous. An **OOM kill is the one
failure the step cannot clean up after itself**: everything else deletes its
own extract and part-built store on the way out. It costs nothing —
`RAIL_DATA_DIR` still holds only whole files, and the next run clears
`.incoming/` before it starts — but if check 3 shows a `run-*` directory,
that is what happened, and `--memory` on the `run` is the fix.

### Rollback

The previous month's release is still there, and installing it is the rollback:

```bash
docker compose run --rm --entrypoint python traxjourney \
    scripts/fetch_rail_data.py --dest /app/data/rail --tag rail-data-2026-09-06
```

It rebuilds every region whose checksum differs from what is installed and
leaves the rest alone, so this converges on the old data without a manual
cleanup. `gh release list --repo rui-nar/TraxJourney | grep rail-data-` lists
the tags to choose from; any of them works however old it is, because a tag
given here is fetched from GitHub by name rather than looked for among the
most recent hundred releases.

The other rollback, when the data itself is suspect rather than one region's:

```bash
# back to Overpass — no image, no data change, effective within 5 minutes
sed -i 's/^RAIL_SOURCE=local/RAIL_SOURCE=overpass/' /opt/traxjourney/.env
docker compose up -d
```

Note that a region dropped from a newer manifest keeps its old store file on
disk, unreferenced and unread — and so does a region that goes from `ok` to
`empty` (upstream stopped publishing rail for it), whose entry stays in the
manifest while its store is no longer read. Coverage drops correctly in both
cases; only the file lingers. Deleting a `*.rail.sqlite` the manifest does not
name as `ok`, together with its `.sha256`, is safe housekeeping; leaving them
is harmless.

## Open items

- [ ] Off-site backups independent of OVH (the VPS's datacenter, Strasbourg,
      is the same site that suffered OVH's 2021 fire — the in-house
      "Automated Backup" option isn't sufficient on its own).
- [x] Decommission the NAS as a deployment target. Validation moved to
      `/opt/traxjourney-val` on the VPS; `deploy.ps1` no longer reaches the NAS.
- [ ] Both environments now share one host, one disk and one 4 GB of RAM. A
      poster render in val competes with prod for memory — worth watching
      before assuming val is free. Confirmed a real constraint, not just a
      theoretical one (issue #209): a route resolution alone measured over
      512M in the API process. Sizing the per-container memory limits in
      `docker-compose.yml.example` generously enough to survive a real
      resolve/poster peak (~3.25 GB for one full stack) leaves too little
      headroom for a second full stack plus the OS on this 4 GB host —
      unresolved; needs either more host RAM or not running both stacks'
      workers at full concurrency simultaneously.
