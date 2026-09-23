# Encounters — people met along the trip (#40)

Log the people you meet on a trip and where/when you met them.

## Concepts
- **Person** — a per-project directory entry. All fields are optional (`name`,
  `email`, `phone`, `notes`, `avatar`, plus **social links**, **nationalities**,
  and a **residence** city); an unnamed person renders as **"Unknown"**. Social
  links are a list of `{network, handle}` (Instagram / Facebook / Polarsteps /
  Strava / …); the legacy `polarsteps` field is kept as a mirror of the
  Polarsteps entry so the shared-trip view still resolves it.
- **Encounter** — links one **person or group** (issue #56) to a **day**, with a
  place (map pin, defaulting to the day's location, editable) and an optional
  note. Rendered as an ordered timeline item, like memories and journal entries.

## Privacy (owner-only)
People and encounters are **never** exposed in shared views (full or no-memories
tokens) — they hold third-party PII. The `GET /api/projects/{name}` owner payload
includes a `people` array + encounter items; the share endpoints strip both, and
the shared geo endpoints never emit encounter pins.

Keyword search runs **client-side** over already-loaded data (person fields +
encounter notes), so it stays compatible with the planned zero-knowledge
encryption work (#26): those text fields are the encrypt-on-write set when E2EE
lands.

## Data model (per-project, Alembic `f40e0c0de001`, extended by `f7e8d9c0b1a2`, `124a1d7b0d32`)
- `person` — `id`, `project_id`, `name?`, `email?`, `phone?`, `polarsteps?`,
  `notes?`, `avatar_photo?`, `created_at`, plus (`f7e8d9c0b1a2`) `socials_json?`,
  `nationalities_json?` (ISO 3166-1 alpha-2 codes), `residence?`.
- `encounter` — `id`, `project_id`, `person_id?`, `group_id?` (`124a1d7b0d32`,
  issue #56), `date`, `time?`, `description?`, `geo_mode`, `lat?`, `lon?`.
  Exactly one of `person_id`/`group_id` is set, enforced at the API layer.
- `projectitem.encounter_id` — FK for encounter timeline items.

Deleting a person cascades to their encounters (+ timeline items); deleting a
group cascades to its group-encounters (+ timeline items) the same way — a
group-referencing encounter has no other entity to fall back to. `.traxj`
export/import round-trips people + groups + encounters (excluded from public
shares).

## API
- People: `POST /api/people`, `GET/PUT/DELETE /api/people/{id}` (the GET returns
  the person's own encounters **plus the ones logged against their group**, see
  Groups below), avatar
  `POST/DELETE /api/people/{id}/avatar`, `GET .../avatar[/thumb]`.
- Encounters: `POST /api/encounters`, `PUT/DELETE /api/encounters/{id}` — the
  body takes either `person_id` or `group_id` (exactly one, issue #56).
- Residence city autocomplete: `GET /api/geo/places?q=` — proxies OSM Nominatim
  server-side (owner-auth; the client debounces) and returns distinct
  "City, Country" labels; only the chosen display string is stored.
- `PUT /api/projects/{name}/items/sort` orders encounters by `date`/`time`.

## Client
- **People** section (manage AppBar → groups icon): searchable list + per-person
  sheet (details, avatar, places/dates met).
- Add an encounter from the day add-item sheet, the "+" speed-dial, or a person;
  the dialog's combined picker lists groups and people in labeled sections
  (issue #56), and a "+" menu creates a new person or group inline.
- Encounters appear inline on their day and as owner-only pins on the manage map.
- **Edit / delete from anywhere they're listed** (issue #175): the trip timeline
  (swipe or the pencil) and the person and group detail sheets (trailing pencil +
  bin). An encounter inherited from a group is editable from a member's sheet too
  — the "With &lt;group&gt;" label marks it as group-wide, and the change applies
  to the group.

## Known limitations
On **mobile web**, the browser's cut/copy/paste for the note fields is
unreliable: Flutter's web build paints text into a canvas, so selection is the
engine's own rather than the browser's, and `Clipboard.getData` maps to
`navigator.clipboard.readText()`, which iOS Safari does not grant to a page. The
person-notes and encounter-note fields therefore carry their own copy/paste
buttons (`note_field_actions.dart`), which don't depend on the selection toolbar.
Paste can still be refused by the browser — the field says so rather than failing
silently. The Android and iOS apps are unaffected.

## Groups (#50)
Bundle several people into a named **group** (e.g. a family or a travel crew).
Like people, groups are per-project and owner-only, and never appear in shared
views.

- **PersonGroup** — an optional `name` plus its own `nationalities` and social
  links (same shape as a person). A person belongs to **at most one** group via
  `person.group_id` (many-to-many is out of scope for v1).
- Data model (Alembic `50c0de5f6a7b`): `person_group` — `id`, `project_id`,
  `name?`, `nationalities_json?`, `socials_json?`, `created_at`; plus the nullable
  `person.group_id` FK. Deleting a group **ungroups** its members (the people
  remain) but deletes its direct group-encounters (see above); `.traxj`
  export/import round-trips groups and membership.
- API: `POST /api/groups`, `GET/PUT/DELETE /api/groups/{id}`, and
  `PUT /api/groups/{id}/members` to set the member list.
- Client: a **People | Groups** directory; the group form reuses the person
  modal's nationality + social-link widgets. Each group tile shows member +
  encounter counts, and the group detail sheet lists its members and its own
  encounters (issue #56), mirroring the person detail sheet.
- **An encounter can reference a group directly** (issue #56, `encounter.group_id`)
  — not just a grouped person (which is still auto-masked as the group on the
  map, `classifyEncounterPin` in `people_search.dart`). Useful when you met the
  group collectively rather than a specific known member.
- **Members inherit their group's encounters** (issue #124): meeting a group
  means meeting its members, so a group-encounter also counts as an encounter
  with everyone in the group. `GET /api/people/{id}` merges both lists (sorted by
  date/time) and tags each row `source: "person" | "group"`, carrying
  `group_id`/`group_name` for the inherited ones so the person sheet can label
  them "With <group>". The People list's per-person encounter count and the
  keyword search over encounter notes follow the same rule
  (`encountersByPerson` in `people_search.dart`). The reverse does **not** hold:
  a group's own encounter list stays limited to its direct encounters.
