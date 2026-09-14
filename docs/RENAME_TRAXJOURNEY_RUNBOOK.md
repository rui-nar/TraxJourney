# ViewTrip → TraxJourney cut-over runbook (issue #151)

The rename PR changes names that live **outside** the repository too: host
directories, the database file, container and service names, the GHCR package,
the Tailscale hostname, Grafana dashboards and several third-party consoles.
Nothing below is automated. The owner runs every step by hand, in this order.

Each step says where it runs:

- **[GitHub]** — github.com, signed in as the repository owner
- **[VPS]** — the OVH VPS, over SSH (`docs/DEPLOYMENT_VPS.md`)
- **[NAS]** — the Synology NAS running the observability stack (`nas/README.md`)
- **[Console]** — a third-party web console
- **[Workstation]** — the Windows dev machine

Old → new names used throughout (the full table is in the #151 plan):

| Old | New |
|---|---|
| repository `rui-nar/ViewTripWeb` | `rui-nar/TraxJourney` |
| image `ghcr.io/rui-nar/viewtripweb` | `ghcr.io/rui-nar/traxjourney` |
| `/opt/viewtrip`, `/opt/viewtrip-val` | `/opt/traxjourney`, `/opt/traxjourney-val` |
| compose service `viewtripweb`, containers `viewtrip-*` | `traxjourney`, `traxjourney-*` |
| `viewtripweb.db` (+ `-wal`, `-shm`), `backups/viewtripweb_*.db` | `traxjourney.db`, `backups/traxjourney_*.db` |
| `VIEWTRIP_ROLE`, `VIEWTRIP_ENV` | `TRAXJOURNEY_ROLE`, `TRAXJOURNEY_ENV` |
| metrics `viewtrip_*`, Alloy `job="viewtrip"`, `service="viewtripweb"` | `traxjourney_*`, `job="traxjourney"`, `service="traxjourney"` |
| Tailscale host `viewtrip-observability` | `traxjourney-observability` |
| Grafana folder `ViewTrip`, uids `viewtrip-*` | `TraxJourney`, `traxjourney-*` |

What is deliberately **not** renamed: the E2EE HKDF strings `viewtrip-e2ee/...`
(protocol bytes; changing them makes every stored key unreadable), the Google
Cloud project id `viewtrip` (ids are permanent), and the old GHCR package
`viewtripweb` (kept as the rollback image source).

---

## A. Before merge

### A1. [VPS] Ingest any project files still only on disk

The PR removes the server's lazy ingest of `.viewtrip`/`.gettracks` files and
deletes `scripts/migrate_to_db.py`. A project that exists only as such a file
would 404 after the upgrade. Check both stacks:

```bash
sudo find /opt/viewtrip /opt/viewtrip-val \( -name '*.viewtrip' -o -name '*.gettracks' \)
```

No output: go to A2. Otherwise ingest them with the **current** image, for
each stack that has any. The script is not in the image (`.dockerignore`
excludes `scripts/` except `fetch_rail_data.py`), so fetch it at the version
that stack runs and bind-mount it in:

```bash
cd /opt/viewtrip                          # or /opt/viewtrip-val
curl -s http://127.0.0.1:8000/api/version # val: port 8001 — note the version
# A release (vX.Y.Z): use the tag. Val ("validation-<short sha>"): use the full
# commit id, from `git rev-parse <short sha>` on the workstation.
REF=vX.Y.Z
curl -fsSLo /tmp/migrate_to_db.py \
  "https://raw.githubusercontent.com/rui-nar/ViewTripWeb/$REF/scripts/migrate_to_db.py"
docker compose run --rm \
  -v /tmp/migrate_to_db.py:/app/scripts/migrate_to_db.py:ro \
  --entrypoint python viewtripweb scripts/migrate_to_db.py
```

It must end with `Done — N migrated, 0 skipped, 0 errors.` Each ingested file
is renamed to `*.migrated`, so re-running the `find` must now print nothing.
It has to be mounted at `/app/scripts/`: the script finds the repo root and
`data/` relative to its own location.

### A2. [VPS] Confirm `DATABASE_URL` is set explicitly

```bash
grep '^DATABASE_URL=' /opt/viewtrip/.env /opt/viewtrip-val/.env
```

Both must be non-empty (e.g. `sqlite:////app/db/viewtripweb.db`). Note the
file each one names; section D renames exactly that file. An empty value would
mean the default file, which changes from `viewtripweb.db` to `traxjourney.db`.

### A3. [GitHub] Rename the repository

Settings → General → Repository name: `ViewTripWeb` → `TraxJourney`.

GitHub redirects the old name (git remotes, API calls, release links, the
`fetch_rail_data.py` default in already-deployed images). **Do not create a
placeholder repository at `rui-nar/ViewTripWeb`**: that destroys every
redirect. The old name is already reserved, because only the owner can create
repositories under `rui-nar`.

Do this immediately before merging, so `REPO_URL` in the new code resolves.

### A4. [Workstation] Point clones at the new URL

```powershell
git -C E:\Dev\ViewTripWeb remote set-url origin https://github.com/rui-nar/TraxJourney.git
git -C E:\Dev\ViewTripWeb remote -v
```

Linked worktrees share this setting. Repeat for any other clone.

### A5. [GitHub] Confirm `GHCR_TOKEN` can create packages

The first push to `ghcr.io/rui-nar/traxjourney` creates a new package, which
needs `write:packages`. Check the PAT behind the `GHCR_TOKEN` Actions secret
(Settings → Developer settings → Personal access tokens): scope
`write:packages` present and not expired.

---

## B. After merge, first image build

### B1. [Workstation] Build `:validation` first

Build the validation image before any release tag, so the new package exists
and its visibility is settled before a VPS pulls from it:

```bash
git fetch origin
git tag -f validation origin/main
git push origin validation --force
```

Wait for "Build and publish Docker image" to succeed.

### B2. [GitHub] Set the new package's visibility

Profile → Packages → `traxjourney` → Package settings. A package created by a
push starts **private**. Making it public is **irreversible**. The VPS pulls
with a `read:packages` PAT, which can pull a private package of the same
owner, so private works for these hosts. Self-hosters can pull it only once it
is public, and the README describes the published image as public.

Check the package page shows it is linked to `rui-nar/TraxJourney` (the
`org.opencontainers.image.source` label does that).

### B3. [GitHub] Keep the `viewtripweb` package

Do not delete or change `ghcr.io/rui-nar/viewtripweb`. Its last tags are the
rollback image (section G). It stops receiving new versions.

### B4. [Workstation] Move the deploy settings into `deploy.env`

`deploy.ps1` is tracked in git since #423, and the host details it used to
hard-code now live in the gitignored `deploy.env`. The main checkout still has
the old, untracked `deploy.ps1` at the same path, and `git pull` refuses to
overwrite an untracked file. Move it out of the checkout first, then pull:

```powershell
Move-Item E:\Dev\ViewTripWeb\deploy.ps1 $HOME\deploy.ps1.pre-423
git -C E:\Dev\ViewTripWeb pull
Copy-Item E:\Dev\ViewTripWeb\deploy.env.example E:\Dev\ViewTripWeb\deploy.env
```

Fill in `deploy.env` from the old script's configuration block:

| Old script | `deploy.env` |
|---|---|
| `$VPS_HOST` | `DEPLOY_HOST` |
| `$VPS_SSH_PORT` | `DEPLOY_SSH_PORT` |
| `$VPS_USER` | `DEPLOY_USER` |
| `$VPS_KEY` | `DEPLOY_SSH_KEY` |
| the `-MapboxToken` default | `MAPBOX_TOKEN` |

Leave `DEPLOY_IMAGE`, `DEPLOY_*_DIR` and `DEPLOY_*_URL` as the example has them.
The old script's `$IMAGE`, `$VPS_BASE` and `$VAL_BASE` are the pre-rename
names, which is what this runbook replaces.

The local `docker-compose.yml` is still gitignored and still says `viewtripweb`
and `/opt/viewtrip`:

```powershell
Select-String -Path E:\Dev\ViewTripWeb\docker-compose.yml -Pattern 'viewtrip' -CaseSensitive:$false
```

Change its image path and compose service name to the new names.

Do not run `deploy.ps1` against a host until that host has been cut over
(section D). It would stop anyway: before building or taking anything down, it
refuses a host whose `docker-compose.yml` does not name
`ghcr.io/rui-nar/traxjourney`. Delete `$HOME\deploy.ps1.pre-423` once a deploy
has passed its checks.

---

## C. NAS / observability (before the VPS)

The dashboards, the Alloy labels and the Tailscale name move together, and the
VPS `.env` files will point at the new Tailscale name, so the NAS goes first.
Old series and log streams stay queryable in Explore until retention expires
(30 days for Prometheus); nothing needs doing about history.

Grafana API calls below run from any machine on the tailnet (the VPS works).
Grafana shares the Tailscale container's network, so it answers on the
tailnet name, which changes in C4:

```bash
G=http://viewtrip-observability:3000        # from C4 on: traxjourney-observability
AUTH="admin:$GRAFANA_ADMIN_PASSWORD"         # the value from the NAS's nas/.env
```

### C1. [NAS] Note what points at the old dashboards

```bash
curl -s -u "$AUTH" "$G/api/folders"                  # note the uid of the "ViewTrip" folder
curl -s -u "$AUTH" "$G/api/org/preferences"          # homeDashboardUID must not be viewtrip-*
curl -s -u "$AUTH" "$G/api/user/preferences"
```

Also check starred dashboards in the UI. Anything pointing at a `viewtrip-*`
uid has to be pointed at its `traxjourney-*` counterpart after C3.

Then list the Grafana alert rules. They live in Grafana's database, not in
the provisioning files, so this PR changes none of them. Two things matter:

- a rule in the `ViewTrip` folder blocks deleting that folder in C3, whatever
  it queries;
- a rule querying `viewtrip_*` or `service="viewtripweb"` sees no data once the
  VPS is cut over, and a "fire on increase" rule then never fires again.

`-f` makes a wrong password or address fail loudly instead of looking like
"no rules":

```bash
curl -fsS -u "$AUTH" "$G/api/folders" > /tmp/folders.json
curl -fsS -u "$AUTH" "$G/api/v1/provisioning/alert-rules" > /tmp/rules.json
python3 - <<'EOF'
import json, re
folders = {f["uid"]: f["title"] for f in json.load(open("/tmp/folders.json"))}
rules = json.load(open("/tmp/rules.json"))
print(len(rules), "alert rule(s)")
for r in rules:
    folder = folders.get(r.get("folderUID"), r.get("folderUID"))
    # queries, and annotations (a panel-linked rule keeps __dashboardUid__ there)
    old = bool(re.search(r"view[ _-]?trip", json.dumps([r.get("data"), r.get("annotations")]), re.I))
    if folder == "ViewTrip" or old:
        print(f"- {r['title']!r} (uid {r['uid']}) folder={folder!r} uses_old_names={old}")
EOF
```

For every rule it lists:

- **now, before C3:** move it out of the `ViewTrip` folder (edit the rule in
  the UI, pick another folder — create `TraxJourney` if it doesn't exist yet);
- **after C3:** if it links to a `viewtrip-*` dashboard (`__dashboardUid__`),
  relink it to the `traxjourney-*` one;
- **after section D:** if its query uses `viewtrip_*` or `service="viewtripweb"`,
  change it to the `traxjourney_*` metric or the `{service="traxjourney"}` stream.

### C2. [NAS] Swap the provisioning with Grafana stopped

In the NAS stack directory:

```bash
docker compose stop grafana
```

Copy from the merged `main` over the NAS copy:

- `nas/grafana/provisioning/dashboards/dashboards.yaml` and the five
  dashboard JSON files (provider `traxjourney`, folder `TraxJourney`, uids
  `traxjourney-*`, `job="traxjourney"` queries);
- `nas/grafana/provisioning/datasources/datasources.yaml`;
- the `nas/docker-compose.yml.example` changes into the NAS's local
  `docker-compose.yml`, including the `tailscale` service's
  `hostname: traxjourney-observability`.

```bash
docker compose start grafana
```

Grafana must be stopped while the files change: it rescans every 30 seconds,
and a scan halfway through the copy provisions a mix of old and new.

### C3. [NAS] Check the old dashboards are gone

The API refuses to delete a provisioned dashboard. With the provider renamed
from `viewtrip` to `traxjourney`, Grafana should delete the orphaned
`viewtrip-*` dashboards itself at startup, but that is not verified on
Grafana 13, so check:

```bash
for uid in viewtrip-http viewtrip-jobs-db viewtrip-integrations-auth viewtrip-logs viewtrip-host-resources; do
  printf '%s ' "$uid"; curl -s -o /dev/null -w '%{http_code}\n' -u "$AUTH" "$G/api/dashboards/uid/$uid"
done                                                   # every line must say 404
curl -s -u "$AUTH" "$G/api/search?tag=viewtrip"        # must be []
```

If any survive, drop their provisioning record and delete them:

```bash
docker compose stop grafana
sqlite3 grafana-data/grafana.db "DELETE FROM dashboard_provisioning WHERE name='viewtrip';"
# no sqlite3 on the NAS:
#   docker run --rm -v "$PWD/grafana-data:/g" alpine:3.20 \
#     sh -c "apk add -q sqlite && sqlite3 /g/grafana.db \"DELETE FROM dashboard_provisioning WHERE name='viewtrip';\""
docker compose start grafana
curl -s -u "$AUTH" -X DELETE "$G/api/dashboards/uid/<uid>"   # per surviving uid
```

Then delete the empty folder, with the uid noted in C1:

```bash
curl -s -u "$AUTH" -X DELETE "$G/api/folders/<ViewTrip folder uid>"
```

### C4. [NAS] Rename the Tailscale host

Loki, Prometheus and Grafana run with `network_mode: service:tailscale`, so
recreate the whole stack, not just the `tailscale` service:

```bash
docker compose up -d --force-recreate
docker compose exec tailscale tailscale status      # must show traxjourney-observability
```

If the machine was ever renamed by hand in the Tailscale admin console, the
hostname setting does not override that: rename it there too (Machines → … →
Edit machine name). Confirm from the VPS:

```bash
curl -s http://traxjourney-observability:3100/ready
curl -s http://traxjourney-observability:9090/-/ready
```

**Optional, to avoid a gap in data:** before this step, point both VPS `.env`
files' `LOKI_PUSH_URL` and `PROMETHEUS_REMOTE_WRITE_URL` at the NAS's `100.x`
Tailscale IP and run `docker compose up -d alloy` in each old stack directory.
The VPS Alloy has no data volume, so whatever it buffered is lost when it
restarts either way. D8 then sets the URLs to `traxjourney-observability`.

Without that, logs and metrics from the VPS stop arriving from here until each
stack's D12.

---

## D. VPS cut-over, per stack: validation first, then production

Run this whole section for **validation** (`/opt/viewtrip-val` →
`/opt/traxjourney-val`, API on `127.0.0.1:8001`), check it, then repeat it for
**production** (`/opt/viewtrip` → `/opt/traxjourney`, `127.0.0.1:8000`).
Validation needs the `:validation` image from B1; production needs a release
tag built by `bump_version_and_release.ps1` from the merged `main`.

The commands use val's names; for prod, drop `-val`.

```bash
OLD=/opt/viewtrip-val
NEW=/opt/traxjourney-val
```

### D1. [VPS] Stop the auto-deploy hook (validation only)

So a validation build cannot trigger a deploy into a half-moved directory:

```bash
sudo systemctl stop webhook 2>/dev/null || true
```

### D2. [VPS] Stop the stack in the OLD directory

```bash
cd "$OLD" && docker compose down
```

It must be `down` from the old directory: that is the only place compose knows
the old project and container names.

### D3. [VPS] Checkpoint the database and keep a pre-cut-over copy

With `DB` set to the host path of the file `DATABASE_URL` names (usually
`db/viewtripweb.db`):

```bash
DB=db/viewtripweb.db
sudo python3 -c "import sqlite3; c=sqlite3.connect('$DB'); print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); c.close()"
ls -l "$DB" "$DB-wal" "$DB-shm" 2>/dev/null   # -wal must be absent or 0 bytes
sudo cp -p "$DB" "db/pre-rename-viewtripweb-$(date +%F).db"
```

`sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE)'` does the same if the
`sqlite3` CLI is installed. The printed tuple must start with `0` (not busy).
The copy is the rollback database: the new release also runs migrations the
old image does not know.

### D4. [VPS] Move the directory

```bash
cd / && sudo mv "$OLD" "$NEW" && cd "$NEW"
```

### D5. [VPS] Rename the database file with its sidecars

```bash
cd "$NEW/db"
sudo mv viewtripweb.db traxjourney.db
for s in -wal -shm; do [ -e "viewtripweb.db$s" ] && sudo mv "viewtripweb.db$s" "traxjourney.db$s"; done
ls -l
```

### D6. [VPS] Rename the backups

The backup service derives the prefix from the database file name, so old
backups are invisible to restore and pruning until renamed:

```bash
cd "$NEW/db/backups"
for f in viewtripweb_*.db; do [ -e "$f" ] && sudo mv "$f" "traxjourney_${f#viewtripweb_}"; done
ls
```

### D7. [VPS] Clear the multiprocess metric files

```bash
sudo rm -f "$NEW"/metrics/*.db
```

They hold samples under the old metric names (`PROMETHEUS_MULTIPROC_DIR`).

### D8. [VPS] Edit `.env`

```bash
cd "$NEW"
sudo cp -p .env ".env.pre-rename"
sudo sed -i \
  -e 's#viewtripweb\.db#traxjourney.db#' \
  -e 's/^VIEWTRIP_ENV=/TRAXJOURNEY_ENV=/' \
  -e 's/^VIEWTRIP_ROLE=/TRAXJOURNEY_ROLE=/' \
  -e 's/viewtrip-observability/traxjourney-observability/g' \
  .env
grep -n -i 'viewtrip' .env    # review anything this still prints
grep -n -E '^(DATABASE_URL|TRAXJOURNEY_ENV|LOKI_PUSH_URL|PROMETHEUS_REMOTE_WRITE_URL)=' .env
```

Check by eye: `DATABASE_URL` names `traxjourney.db`, `TRAXJOURNEY_ENV` is
`validation` (prod: `production`), and both push URLs use
`traxjourney-observability` (also if C4's optional step set them to an IP).
`TRAXJOURNEY_ENV` and the new Alloy config (D10) must go live together: the
new config reads `TRAXJOURNEY_ENV`, and without it the `env` label is empty and
prod and val data collide. If `MAIL_FROM` carries a display name, change it
to `TraxJourney` here too.

### D9. [VPS] Edit the host's `docker-compose.yml`

```bash
sudo cp -p docker-compose.yml docker-compose.yml.pre-rename
grep -n -i 'viewtrip' docker-compose.yml
```

Change, compared against `docker-compose.yml.example` on the merged `main`:

- add a top-level `name: traxjourney-val` (prod: `name: traxjourney`), so the
  compose project name no longer depends on the directory;
- every `image: ghcr.io/rui-nar/viewtripweb:…` → `ghcr.io/rui-nar/traxjourney:…`
  (same tag: `:validation` here, `:latest` on prod);
- service `viewtripweb:` → `traxjourney:`, and every `depends_on` entry naming it;
- `VIEWTRIP_ROLE: worker` → `TRAXJOURNEY_ROLE: worker`;
- container names: replace the `viewtrip` prefix with `traxjourney` and keep
  any `-val` part, e.g. `viewtrip-val-alloy` → `traxjourney-val-alloy`. The
  Alloy container must match `^/traxjourney-(val-)?alloy$`, or its own logs
  are labelled wrongly;
- validation's `db-seed` service (`docs/DEPLOYMENT_VPS.md` §5): the mount
  `/opt/traxjourney/db/backups:/prod-backups:ro`, the glob
  `/prod-backups/traxjourney_*.db`, and `/db/traxjourney.db` (plus its `-wal`
  and `-shm`) in the `rm` and `cp` lines. Do not run the seed until prod has
  been cut over too: the mount path does not exist before that;
- any absolute `/opt/viewtrip…` volume path.

```bash
grep -n -i 'viewtrip' docker-compose.yml   # must print nothing
docker compose config --quiet && echo compose ok
```

### D10. [VPS] Re-copy the Alloy config

The metric job, service label, scrape target and container regex all changed.
Take the example from the merged `main`, then re-apply any local edits you had
(diff against the backup):

```bash
sudo cp -p config/alloy-config.river config/alloy-config.river.pre-rename
curl -fsSLo /tmp/alloy-config.river \
  https://raw.githubusercontent.com/rui-nar/TraxJourney/main/config/alloy-config.river.example
diff config/alloy-config.river.pre-rename /tmp/alloy-config.river
sudo cp /tmp/alloy-config.river config/alloy-config.river
```

### D11. [VPS] Update the webhook (validation only)

```bash
cd "$NEW/webhook"
for f in deploy-validation.sh webhook.service; do
  sudo curl -fsSLo "$f" "https://raw.githubusercontent.com/rui-nar/TraxJourney/main/vps/webhook/$f"
done
sudo chmod +x deploy-validation.sh
sudo sed -i 's#/opt/viewtrip-val#/opt/traxjourney-val#g' hooks.yaml   # keeps the secret
grep -n viewtrip hooks.yaml deploy-validation.sh webhook.service     # must print nothing
sudo cp webhook.service /etc/systemd/system/webhook.service
sudo systemctl daemon-reload && sudo systemctl restart webhook
sudo systemctl status webhook --no-pager
```

Skip this step if the webhook was never installed.

### D12. [VPS] Pull and start

```bash
cd "$NEW"
docker compose pull && docker compose up -d
docker compose ps
docker compose logs --since 5m traxjourney | tail -n 50
```

A log line `Refusing to start: found the old database file` means the
container's working directory still has a `viewtripweb.db` and `DATABASE_URL`
is empty; recheck D5 and D8.

### D13. [VPS/NAS] Verify

```bash
PORT=8001   # prod: 8000
curl -s http://127.0.0.1:$PORT/api/version
TOKEN=$(sudo grep '^METRICS_TOKEN=' .env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/metrics | grep -c '^traxjourney_'   # > 0
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/metrics | grep -c '^viewtrip_'      # 0
```

(`/metrics` is disabled when `METRICS_TOKEN` is empty; skip those two then.)

In Grafana on the NAS (Explore):

- Prometheus: `up{job="traxjourney"} == 1` for `env="validation"` (and
  `production` once prod is done), and `count by (env)(up{job="traxjourney"})`
  lists them;
- Prometheus: `traxjourney_http_requests_total` has data;
- Loki: `{service="traxjourney", env="validation"}` returns fresh lines;
- the five `TraxJourney / ...` dashboards have a populated `env` dropdown.

In the browser: sign in on val.traxjourney.com (prod: traxjourney.com), open a
trip, and export it as `.traxj`.

### D14. Things that do not carry over

- **Admin log-level override.** Its Redis key moved from
  `viewtrip:log_level_override` to `traxjourney:log_level_override`. An
  override active before the cut-over is dropped; set it again from the admin
  page if you still need it.
- **Old Redis keys** (`viewtrip:*`) are orphaned. They are harmless. To remove
  them:
  ```bash
  docker compose exec redis sh -c "redis-cli --scan --pattern 'viewtrip:*' | xargs -r redis-cli del"
  ```
- **Signed-in web and Android users** stay signed in: the client moves its
  token from `viewtrip_jwt` to `traxjourney_jwt` on first start.

When prod is done too, delete the `*.pre-rename` files and the
`db/pre-rename-viewtripweb-*.db` copies once you no longer need a rollback.

---

## E. Consoles

Any time after merge. None of these affects running code.

- **[Console] Strava API app** — strava.com/settings/api → Application Name
  `TraxJourney` (and val's own Strava app, if it has one). The callback domain
  does not change.
- **[Console] Google OAuth consent screen** — console.cloud.google.com, project
  id `viewtrip` → APIs & Services → OAuth consent screen / Branding → App name
  `TraxJourney`. A verified app may need re-verification after a name change.
  IAM & Admin → Settings → Project name can also read `TraxJourney`; the id
  `viewtrip` cannot change.
- **[Console] Google Play** — Play Console: check the store listing and app
  name read `TraxJourney`.
- **[Console] Stripe** — in the live account **and** the sandbox: Settings →
  Business → Public business name `TraxJourney`; Settings → Payments →
  Statement descriptor; Settings → Billing → Customer portal → headline.
  `scripts/stripe_catalog.py` only sets the portal headline when it creates
  the configuration, so the existing one must be changed here.
- **[Console] Mail provider** — the sender display name for `MAIL_FROM` (see D8).
- **[GitHub] Repository description** — currently "GetTracks web app — Reflex +
  Docker for Synology NAS":
  ```bash
  gh repo edit rui-nar/TraxJourney --description "TraxJourney — build and share multi-sport trip maps from Strava and Polarsteps. FastAPI + Flutter, self-hostable with Docker."
  ```
- **[Console] iOS Google Sign-In** — the iOS bundle id is now
  `com.traxjourney.app`, but `flutter_client/ios/Runner/GoogleService-Info.plist`
  still carries a `CLIENT_ID`/`REVERSED_CLIENT_ID` registered to the old bundle
  id. In Firebase (or Google Cloud → Credentials → Create OAuth client ID →
  iOS) register an iOS app for `com.traxjourney.app`, download its
  `GoogleService-Info.plist`, and replace the file in a PR. Until then Google
  Sign-In on iOS fails; email/password login still works.

---

## F. Dev workstation (last)

1. **Merge or prune the worktrees** — the sibling `E:\Dev\ViewTripWeb-*`
   worktrees and the agent worktrees under `E:\Dev\ViewTripWeb\.claude\worktrees`:
   ```powershell
   git -C E:\Dev\ViewTripWeb worktree list
   git -C E:\Dev\ViewTripWeb worktree remove <path>     # for each one you no longer need
   git -C E:\Dev\ViewTripWeb worktree prune
   ```
2. **Rename the checkout** and repair the links of the worktrees you kept.
   Close VS Code, terminals and anything holding files open first.
   ```powershell
   Rename-Item E:\Dev\ViewTripWeb TraxJourney
   git -C E:\Dev\TraxJourney worktree repair <each kept worktree path>
   git -C E:\Dev\TraxJourney worktree list
   ```
   Worktrees under `.claude\worktrees` moved with the checkout; pass their new
   paths to `repair` too.
3. **Move the Claude Code memory directory**:
   ```powershell
   Get-ChildItem C:\Users\rui_n\.claude\projects | Where-Object Name -like '*ViewTripWeb*'
   Rename-Item C:\Users\rui_n\.claude\projects\e--Dev-ViewTripWeb e--Dev-TraxJourney
   ```
   Do the same for an upper-case `E--Dev-ViewTripWeb` directory if one exists.
   Memory files that name old paths or containers need a pass by hand.
4. **Recreate `.venv`** (its scripts hold absolute paths):
   ```powershell
   cd E:\Dev\TraxJourney
   Remove-Item -Recurse -Force .venv
   py -3.14 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
5. **Recreate the test containers.** The Flutter one mounts the old path, so it
   must be recreated (`tools/flutter-test/README.md`):
   ```powershell
   docker rm -f viewtrip-flutter-3.47.1-tests
   docker run -d --name traxjourney-flutter-3.47.1-tests --restart unless-stopped `
     -v "E:/Dev/TraxJourney/flutter_client:/src:ro" flutter-3471
   ```
   The Python CI-parity container has no mount, so a rename is enough:
   ```powershell
   docker rename viewtrip-py314-citest traxjourney-py314-citest
   ```
   Remove the per-worktree `traxjourney-flutter-151*` containers once their
   worktrees are gone.
6. **Fix the workspace file** `E:\Dev\ViewTripWeb.code-workspace`: rename it to
   `TraxJourney.code-workspace` and change the folder paths inside it to
   `E:\Dev\TraxJourney`.
7. **Rename local database files** — the server refuses to start next to a
   `viewtripweb.db` when `DATABASE_URL` is unset:
   ```powershell
   cd E:\Dev\TraxJourney
   Get-ChildItem -Recurse -File -Filter 'viewtripweb*' -Exclude *.py |
     Where-Object FullName -notmatch '\\(\.venv|\.git|flutter_client)\\' |
     Rename-Item -NewName { $_.Name -replace '^viewtripweb', 'traxjourney' } -WhatIf
   ```
   Check the list, then run it again without `-WhatIf`. That covers
   `viewtripweb.db`, its `-wal`/`-shm` and `backups\viewtripweb_*.db`.
8. **Refresh the knowledge graph**: `graphify update .`

---

## G. Rollback

Per stack, while the other stays as it is. Production data written after the
cut-over is lost by step 3, so decide quickly.

1. **[VPS]** `cd /opt/traxjourney-val && docker compose down` (prod: `/opt/traxjourney`).
2. **[VPS]** Reverse the move: `cd / && sudo mv /opt/traxjourney-val /opt/viewtrip-val`.
3. **[VPS]** Restore the database. The new release migrated the schema, and
   the old image cannot run against a newer revision, so use the D3 copy:
   ```bash
   cd /opt/viewtrip-val/db
   sudo rm -f traxjourney.db traxjourney.db-wal traxjourney.db-shm
   sudo cp -p pre-rename-viewtripweb-<date>.db viewtripweb.db
   cd backups && for f in traxjourney_*.db; do [ -e "$f" ] && sudo mv "$f" "viewtripweb_${f#traxjourney_}"; done
   ```
4. **[VPS]** Put back the files saved in D8–D10:
   ```bash
   cd /opt/viewtrip-val
   sudo mv .env.pre-rename .env
   sudo mv docker-compose.yml.pre-rename docker-compose.yml
   sudo mv config/alloy-config.river.pre-rename config/alloy-config.river
   sudo rm -f metrics/*.db
   ```
5. **[VPS]** Pin the old image. `ghcr.io/rui-nar/viewtripweb:latest` is the last
   release before the rename; pin its tag (e.g. `:vX.Y.Z`) rather than a
   floating one. For val, use a release tag too: `:validation` of the old
   package may be newer than the database copy.
6. **[VPS]** `docker compose pull && docker compose up -d`, then check
   `/api/version`.
7. **[VPS]** Validation only: point the webhook back (`sed` `/opt/traxjourney-val`
   → `/opt/viewtrip-val` in `hooks.yaml`, `deploy-validation.sh` and
   `/etc/systemd/system/webhook.service`; `daemon-reload` and restart), or
   leave it stopped.
8. **[NAS]** Only if both stacks roll back: restore the previous
   `nas/grafana/provisioning` and Tailscale hostname, the reverse of section C.

Client-side effects of a rollback: anyone who exported a `.traxj` file cannot
import it into the old version, and users whose app already moved its token to
`traxjourney_jwt` sign in again once.

The GitHub repository rename is not rolled back. The redirects make the old
name keep working for the old image.
