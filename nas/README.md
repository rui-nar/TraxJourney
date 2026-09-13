# NAS observability stack — deployment runbook

Step-by-step for deploying Loki + Prometheus + Grafana on this NAS
(issue #205). For the *why* behind the design (Loki vs. Mimir, retention,
the queries you'll actually run) see `../docs/OBSERVABILITY.md`. For the
VPS-side half that pushes to this stack, see `../docs/DEPLOYMENT_VPS.md`'s
"Observability" section.

Everything here assumes Synology DSM + Container Manager, since that's what
this NAS runs (see `../docs/DEPLOYMENT_VPS.md` §4 for the historical NAS
paths). Adjust paths if that's changed.

**None of this has been run against your actual NAS from this session** — I
don't have access to it. Treat every command below as something to verify
step by step, not paste-and-walk-away.

## 0. Before anything else: check TUN device support

The sidecar networking pattern (`network_mode: service:tailscale` for Loki/
Prometheus/Grafana) needs `/dev/net/tun` to exist and the `tun` kernel
module to be loadable. Most DSM 7.x x86_64 models have this out of the box;
lower-end ARM models sometimes don't. SSH in and check:

```bash
ls -la /dev/net/tun
lsmod | grep tun
```

If both exist, skip to §1. If `/dev/net/tun` is missing, try:

```bash
sudo mkdir -p /dev/net
sudo mknod /dev/net/tun c 10 200
sudo chmod 600 /dev/net/tun
sudo modprobe tun
```

If `modprobe tun` fails (module not present in this DSM build for your
model), the sidecar pattern isn't available here — see "Fallback: no TUN
device" at the bottom of this file before going any further with the
compose file as written.

## 1. Generate a Tailscale auth key

https://login.tailscale.com/admin/settings/keys → Generate auth key.
Reusable (not tied to your personal login), and either non-expiring or with
a calendar reminder to rotate it before it expires — an expired key means
the container silently can't rejoin the tailnet on its next restart. Tag it
`tag:observability` if your tailnet uses ACL tags, otherwise leave default.

## 2. Deploy

Loki, Prometheus and Grafana all drop root and run as a fixed non-root UID.
Docker creates each bind-mounted data directory fresh on first `up`, owned
by whoever ran the command — not by the container's UID — so each one starts
out unwritable to the process that needs it, and the container crash-loops
on "permission denied" until it's fixed. Create and `chown` them *before*
the first `up` rather than discover this one container at a time:

```bash
cd nas/
cp docker-compose.yml.example docker-compose.yml
cp .env.example .env
# edit .env: TS_AUTHKEY (from §1), GRAFANA_ADMIN_PASSWORD (pick one)

mkdir -p loki-data prometheus-data grafana-data
sudo chown -R 10001:10001 loki-data        # Loki's image UID
sudo chown -R 65534:65534 prometheus-data  # Prometheus's image UID ("nobody")
sudo chown -R 472:472 grafana-data         # Grafana's image UID

docker compose up -d
docker compose logs -f tailscale   # confirm it joins the tailnet cleanly
```

Watch for the Tailscale container logging that it authenticated and got an
IP — if `TS_AUTHKEY` is wrong or expired, it'll sit retrying instead.

**If a container still crash-loops on "permission denied" despite the
`chown` above** — check for a DSM ACL overriding the plain POSIX
permissions (common on Btrfs volumes, and especially likely if this
directory has ever hosted another container's data, since DSM ACLs are
inherited from the parent folder):

```bash
ls -la loki-config.yaml   # a trailing `+` on the mode means an ACL is present
synoacltool -get loki-config.yaml
```

A `+` with no "everyone"/generic-allow entry in the ACL list means DSM is
denying access to the container's UID regardless of what the POSIX bits
say. Strip it back to plain POSIX permissions for the specific file or
directory that's failing:

```bash
synoacltool -del loki-config.yaml
```

(There's no `-disable` verb, despite what you might expect — `-del` with no
index removes the whole ACL. `synoacltool -h` lists the rest if you need
finer-grained control, e.g. adding an explicit "everyone: read" entry
instead of removing the ACL outright.)

## 3. Confirm the tailnet name

```bash
docker compose exec tailscale tailscale status
```

Note the IP or MagicDNS name for `traxjourney-observability` — that's what the
VPS's `LOKI_PUSH_URL`/`PROMETHEUS_REMOTE_WRITE_URL` point at (§4). From
another device already on the same tailnet, sanity-check reachability:

```bash
curl -s http://traxjourney-observability:3100/ready   # Loki
curl -s http://traxjourney-observability:9090/-/ready  # Prometheus
```

Open Grafana at `http://traxjourney-observability:3000` (only reachable from a
device on the tailnet — that's the point) and confirm the Prometheus and
Loki datasources (auto-provisioned from
`grafana/provisioning/datasources/datasources.yaml`) show green in
Settings → Data sources.

Five dashboards are auto-provisioned too (`grafana/provisioning/dashboards/`,
read-only — edit the JSON and it picks up the change within 30s, no restart
needed), in a **TraxJourney** folder: **HTTP & Traffic**, **Jobs & Database**,
**Integrations & Auth**, **Logs**, and **Host Resources** (VPS memory,
swap, CPU, load and disk — added after issue #209, see
`../docs/DEPLOYMENT_VPS.md`'s Alloy section for the host `/proc`/`/sys`
mounts it depends on). Panels are a direct rendering of the metrics/queries
already documented in `../docs/METRICS.md` and `../docs/OBSERVABILITY.md`'s
"Queries an operator actually runs" — nothing in them is new, they just save
re-typing the same PromQL/LogQL each time. Empty until §4 below has data
flowing.

Every dashboard has an `env` variable (top-left) — prod and val push to this
same Grafana, so pick which one you're looking at. See
`../docs/OBSERVABILITY.md`'s "Prod and val share this Grafana" section.

## 4. Point Alloy (VPS side) at this stack

On the VPS (see `../docs/DEPLOYMENT_VPS.md`'s "Observability" section for
the full VPS-side setup — Tailscale install there, `config/alloy-config.river`,
etc.), set in its `.env`:

```
LOKI_PUSH_URL=http://traxjourney-observability:3100/loki/api/v1/push
PROMETHEUS_REMOTE_WRITE_URL=http://traxjourney-observability:9090/api/v1/write
```

(Use whichever hostname/IP §3 showed you — MagicDNS name is more stable
across IP churn than the raw `100.x.y.z` address.) Then
`docker compose up -d` the `alloy` service on the VPS and watch for log
lines and `traxjourney_*` metrics starting to arrive in Grafana here within a
minute or two.

## 5. Retention / disk

`loki-config.yaml` and the `prometheus` command in `docker-compose.yml.example`
both default to 30 days, matching `src/backup/backup_service.py`'s existing
backup-retention convention. "The NAS has disk to spare" is not the same as
"unbounded is fine" — revisit these numbers after you've seen a few weeks of
real volume, not before.

## Fallback: no TUN device

If §0 ruled out the sidecar pattern for your NAS model, drop
`network_mode: service:tailscale` from the `loki`/`prometheus`/`grafana`
services, remove the `tailscale` service and its `depends_on` entries
entirely, install Tailscale natively on the NAS instead (DSM's
Package Center, or SynoCommunity if DSM doesn't ship it directly), and
either:

- bind each service's `ports:` to the NAS's Tailscale IP specifically
  (`<tailscale-ip>:3100:3100` etc.) if your Docker/DSM version supports
  binding a published port to a non-default interface, or
- publish normally and rely on the NAS's own firewall to block everything
  except the tailnet — verify this from *outside* your LAN, not just by
  reading the firewall rule, since Docker's own NAT can bypass an
  underlying firewall the same way `../docs/DEPLOYMENT_VPS.md` §1 already
  documents for the VPS.

The sidecar pattern is meaningfully simpler to get right (there's no rule
to misconfigure — the ports just aren't on the LAN-facing bridge at all),
so it's worth confirming TUN support properly before defaulting to this
path.
