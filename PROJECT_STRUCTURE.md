# TraxJourney — Project Structure

## Directory Layout

```
TraxJourney/
│
├── api/                          # FastAPI route handlers (mounted in router.py)
│   ├── router.py                 # FastAPI app: mounts routers, lifespan, SPA fallback
│   ├── deps.py                   # JWT dependency (get_current_user, create_access_token)
│   ├── auth.py                   # Auth endpoints (local + Google → JWT)
│   ├── admin.py                  # Admin dashboard endpoints (is_admin-gated)
│   ├── projects.py               # Project CRUD, day-meta, track style, sync-meta
│   ├── project_access.py         # Shared (caller, name, ?owner) → DBProject resolver + role gating
│   ├── project_shared.py         # Shared infra (no routes): ProjectRepo instance, project-file directory, background tasks
│   ├── project_items.py          # Item delete/reorder/sort
│   ├── project_shares.py         # Share-token create/revoke, per-share content key, visitor stats
│   ├── project_transfer.py       # Import/export (.traxj, GPX, ZIP)
│   ├── activities.py             # Add/refresh/track-edit/split/delete Strava activities
│   ├── segments.py               # Connecting transport segments (flight/train/bus/boat)
│   ├── geo.py                    # GeoJSON builders (full + low-res)
│   ├── memories.py               # Memory CRUD, photos, comments, likes, translations
│   ├── journal.py                # Journal entry CRUD + photos (per-author, private)
│   ├── members.py                # Travel-companion invites (viewer/editor/co-owner), member list/removal
│   ├── people.py                 # Per-project people directory CRUD, avatars
│   ├── groups.py                 # Groups of people CRUD + membership
│   ├── encounters.py             # People-you-met CRUD, tied to a day/place
│   ├── encryption.py             # Zero-knowledge E2EE enable/status, device register/approve, recovery
│   ├── share.py                  # Public read-only share endpoints + tiles
│   ├── strava.py                 # Strava OAuth + activity browsing/sync
│   ├── polarsteps.py             # Polarsteps connect + trip/step listing
│   ├── poster.py                 # A0 trip poster generation (async job + status/download)
│   ├── backup.py                 # List/restore database backups
│   └── translations.py           # Google Translate helper (used by memory endpoints)
│
├── models/                       # SQLModel database models
│   ├── db.py                     # Engine + get_session() context manager
│   ├── project_db.py             # All project-domain tables (see Schema below)
│   └── user.py                   # LocalUser, UserInfo, StravaToken, PolarstepsToken
│
├── src/                          # Core business logic (shared by API and tests)
│   ├── admin/                    # Admin dashboard queries
│   ├── api/
│   │   ├── strava_client.py      # StravaAPI — HTTP client with retry + rate limiting
│   │   └── polarsteps_client.py  # Polarsteps trip/step fetch client
│   ├── auth/                     # OAuth2 session, callback handler, token store
│   ├── backup/
│   │   └── backup_service.py     # SQLite online backup / restore / prune (30-day)
│   ├── cache/
│   │   └── activity_cache.py     # Per-user Strava activity cache
│   ├── config/settings.py        # Config — dot-notation access to config/config.json
│   ├── email/                    # Transactional email — EmailService (SMTP/console backends)
│   │   └── templates/            #   + Jinja text/html templates, rendered by pure functions
│   ├── exceptions/errors.py      # Custom exception hierarchy
│   ├── filters/filter_engine.py  # FilterCriteria + FilterEngine.apply()
│   ├── gpx/processor.py          # GPX export
│   ├── models/                   # Domain models: activity, project, memory, journal,
│   │                             #   track, great_circle (SLERP arc for segments)
│   ├── poster/                   # A0 poster layout, map stitching, job runner
│   ├── project/
│   │   ├── project_io.py         # ProjectIO — (de)serialise .traxj JSON
│   │   └── project_repo.py       # ProjectRepo — DB-backed CRUD (optimistic locking)
│   ├── services/
│   │   ├── hafas_service.py      # Train schedules (DB/ÖBB/DSB/VR digitraffic)
│   │   └── overpass_service.py   # OSM rail/ferry/bus route geometry
│   ├── tile_renderer.py          # Raster tile cache for share links / posters
│   └── utils/logging.py          # Logger setup
│
├── flutter_client/               # Flutter frontend (web / Android / iOS)
│   ├── lib/
│   │   ├── main.dart             # App entry point + provider setup
│   │   └── src/
│   │       ├── api/              # ApiClient — HTTP + auth headers
│   │       ├── auth/            # Login/register + auth state
│   │       ├── core/            # app_router.dart (go_router + auth guard)
│   │       ├── map/            # Shared map utilities + location picker
│   │       ├── projects/      # Main project screen, map panel, stats, memories,
│   │       │                  #   image export, social share dialog
│   │       ├── settings/      # Settings screen (incl. backup restore)
│   │       ├── share/        # Pure social-share units + platform edges
│   │       └── shared/       # Read-only shared-project view
│   └── pubspec.yaml
│
├── alembic/                      # Database migrations
│   └── versions/
│
├── scripts/                      # One-off scripts (icons, release, version)
├── config/
│   ├── config.example.json       # Template — copy to config.json and fill in credentials
│   └── config.json               # Gitignored — Strava + Google credentials
│
├── assets/                       # App icons / static assets
├── tests/                        # Python test suite (pytest)
│
├── alembic.ini                   # Alembic configuration
├── Dockerfile                    # Container image (FastAPI + uvicorn + bundled web build)
├── entrypoint.sh                 # Container entrypoint
├── requirements.txt              # Python dependencies
├── deploy.ps1                    # Gitignored — build + push image + deploy to NAS
├── PROJECT_STRUCTURE.md          # This file
└── README.md                     # Getting started guide
```

> `docker-compose.yml` is gitignored (host-specific paths + env); CI publishes
> the image to `ghcr.io/rui-nar/traxjourney`.

## Key Concepts

### Data Storage

All data lives in **SQLite via SQLModel** (file defaults to `traxjourney.db`,
overridable with `DATABASE_URL`). Migrations are managed by Alembic and run
automatically on startup (`alembic upgrade head` in the FastAPI lifespan).

Projects are persisted as relational rows (project + ordered items + activities
+ memories + journals + segments). The `.traxj` format (the only one accepted
on import) is the JSON serialisation used for import/export only,
produced by `ProjectIO`. Photos are stored on disk under the data volume,
referenced by UUID from the memory/journal rows.

A daily backup of the SQLite file is taken at 02:00 UTC (APScheduler), kept for
30 days, and restorable from the settings screen.

### Auth Flow

1. User logs in via Google (`POST /api/auth/google`) or local email/password (`POST /api/auth/token`)
2. Server verifies credentials → creates/fetches `UserInfo` row → returns signed JWT
3. Flutter stores the JWT and sends it as `Authorization: Bearer` on every API call
4. Strava/Polarsteps OAuth: the user's JWT is passed as the OAuth `state` param so the stateless callback can resolve the user, then a token row is stored

### GeoJSON & Route Resolution

`GET /api/geo/project` produces a GeoJSON FeatureCollection: activity tracks
decoded from `summary_polyline` (or a straight `start→end` fallback), and
connecting segments. Transport segments (train/ferry/bus) have their real-world
geometry resolved asynchronously via `hafas_service` (schedules) and
`overpass_service` (OSM rail/ferry/bus ways), falling back to a great-circle arc.

### Sharing

Read-only public links come in two flavours (`share_token`, with memories, and
`share_token_no_memories`). A memory can be deep-linked via
`/share/<token>?memory=<public_id>`, where `public_id` is a stable per-memory
UUID independent of the primary key, so links survive re-import.

### Travel Companions

A trip owner invites other accounts via a multi-use link (`projectinvite`,
consumed into a `projectmember` row on accept) carrying one of four tiers —
viewer (read-only), editor (content mutations), co-owner (editor + rename/
share-links/member-management), and the implicit owner. `api/project_access.py`
centralises the `(caller, name, ?owner) → DBProject` resolution and role check
every route uses. Each companion's journal entries stay private to them
(`journalentry.user_info_id`); invite links are copyable or, once SMTP is
configured (`src/email/`), emailed directly to the invitee.

### Database Schema (SQLModel + Alembic)

| Table | Purpose |
|---|---|
| `localuser` | Local account: username, bcrypt hash, enabled flag |
| `userinfo` | Identity: local/google link, email, display name, avatar, provider |
| `stravatoken` | Per-user Strava OAuth tokens |
| `polarstepstoken` | Per-user Polarsteps token |
| `project` | A trip: name, dates, filter/day-meta/stats/counters JSON, track style, share tokens, lock_version |
| `projectsyncmeta` | Per-project Strava/Polarsteps sync configuration + timestamps |
| `projectitem` | Ordered item (activity / segment / memory / journal) with position |
| `activity` | Cached Strava/GPX activity (geometry, elevation, inline fields) |
| `memory` | Day annotation: public_id, date, text, photos, geo, like/comment counts |
| `memory_comment` | Threaded comments on a memory |
| `memory_like` | Likes on a memory |
| `memory_translation` | Cached translations of a memory's text |
| `journalentry` | Private day note with photos (`user_info_id` — per-author, private) |
| `projectmember` | Travel-companion membership: user, role (viewer/editor/co-owner), invited_by |
| `projectinvite` | Invite-link token → role granted on accept |
| `sharevisit` | Visitor analytics for share links |
| `stravacache` | Per-user Strava activity-list cache |

> This table covers the core trip/sharing schema; people/groups/encounters,
> E2EE key material, and poster-job tables also exist — see `models/project_db.py`
> and `models/user.py` for the full set.
