# Admin dashboard (issue #25)

An admin-only page giving operators a high-level view of a deployment: user
count + recent sign-ups, per-user storage, project/activity/memory counts,
aggregate totals, each user's billing plan and chosen encryption tier — plus a
user search, a tier-gated password reset, user deletion, a live log-level
switch and a broadcast email.

Reach it at the admin-panel icon in the projects-screen app bar (shown only to
admins), **Settings → Admin → "Open admin dashboard"**, or the `/admin` route
directly.

## Who is an admin
- **Seeded account:** on startup, if no `admin` user exists, one is created with
  username `admin` / password `admin`. It is flagged `is_admin` and
  **must change its password on first login** (the app blocks everything else
  until it does). ⚠️ **Change this password before exposing the instance.**
- **`ADMIN_EMAILS`** (optional): a comma-separated, case-insensitive list of
  emails; matching accounts are promoted to admin on login. Use this to make
  your own real account an admin.
- **Granted from the dashboard:** an admin can promote/demote any user found
  via the search box ("Make admin" / "Remove admin"). An admin cannot revoke
  their own access — this guarantees there's always at least one admin left
  to undo a mistake. Note that `is_admin` is baked into the JWT at login time,
  so a promoted/demoted user must log out and back in for the change to take
  effect client-side (the server-side `/api/admin/*` gate always re-checks the
  DB, so revocation is enforced immediately regardless).

Access is enforced server-side: every `/api/admin/*` route requires an admin
(`require_admin`, re-checked against the DB) and returns **403** otherwise.

## What it shows — and what it deliberately does NOT
Only **metadata**: counts, on-disk sizes, emails/display names, signup dates,
billing plan, and encryption tier. It **never** shows memory or journal content. This keeps it
compatible with the (separate) end-to-end-encryption work: E2EE hides *content*,
and the dashboard only ever surfaces *metadata*.

Storage per user is the sum of file sizes under `data/users/{id}/`. The walk is
**cached** and computed off the request's DB path, with an explicit
**"Recalculate storage"** action — so a dashboard load never holds a database
connection during a slow filesystem scan.

## User search + password reset
Search users by email / username / display name, then optionally reset a user's
password (issues a random temporary password with forced-change-on-login).

**Reset is only allowed when the user's data is admin-recoverable** — encryption
tier **None or Low**. For **Medium / High** it is **blocked (409)**: those tiers
are zero-knowledge, so an admin reset could never restore the user's encrypted
data — only the user's own recovery can. (The reset only ever changes the login
password; it never decrypts anything.) Google-login accounts have no server
password and cannot be reset. Resets are logged.

## Encryption tier (None / Low / Medium / High)
Meant to be derived from the E2EE tables via `user_encryption_tier`
(`src/admin/tiers.py`). **E2EE has since landed** (`UserInfo.encryption_enabled`,
`DBRecoveryWrap.method`), but the helper is **still a stub returning `"none"`**,
so the tier column shows `none` for everyone and the reset gate above never
blocks — not even for an account with Medium/High encryption. Wiring it means
feeding those two columns through the existing pure mapping `tier_from`
(`escrow` → low, `qna` → medium, `recovery_key`/`passphrase` → high), which
lights up both the tier column and the reset gate.

## Other admin tools
- **Plan column** (Users table and search results): the user's billing plan,
  marked "Granted" when an admin override set it, with a link to the Stripe
  customer when there is one. Granting or clearing an override
  (`PUT /api/admin/users/{id}/plan`) and the owed-refunds list are API-only — see
  [BILLING.md](BILLING.md).
- **Delete user** (search results): permanently deletes the account and all its
  data. An admin cannot delete their own account here (409).
- **Logging** card: apply DEBUG / INFO / WARNING / ERROR for 15 min, 1 hour,
  4 hours or indefinitely, without a restart, or revert to the `LOG_LEVEL`
  baseline (`/api/admin/log-level`). A restart also drops the override.
- **Send email** card: a plain-text email to the selected users, or to all.
- **Backups** card — in **Settings**, not on the dashboard, shown to admins only:
  pick one of the daily database backups (taken at 02:00 UTC, last 30 kept) from
  the drop-down and **Restore** it, which replaces the entire database
  (`/api/backup/`, admin-only).
