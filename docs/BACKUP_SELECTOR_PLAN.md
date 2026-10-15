# Backup drop-down selector — Plan for #625

## Problem

The admin-only **Backups** card on the Settings screen shows every retained
daily backup as its own row (date, size, Restore button). Retention is 30 days,
so an admin sees up to 30 rows, and everything below the card (Encryption, …)
is pushed far down the page. Admins restore rarely; the card takes the most
space on the screen for the least-used action.

## Current state

- `flutter_client/lib/src/settings/settings_screen.dart`
  - State: `_backups` (list of `{date, size_bytes}`), `_backupsLoading`,
    `_backupsRequested`, `_restoringDate` (~L105-109).
  - `_loadBackups()` (~L499) fetches through `SettingsService.listBackups()`.
  - `_restoreBackup(date)` (~L511) shows the confirmation dialog, sets
    `_restoringDate`, calls the service, then `reloadApp()` on success or shows
    a "Restore failed" SnackBar on failure.
  - The card (~L1004-1062): loading spinner / empty-state text / an intro line
    plus `_backups.map(...)` producing one `Row` per backup, the size label
    formatted inline (KB under 1 MB, otherwise MB with one decimal).
- `GET /api/backup/` (`api/backup.py`, `src/backup/backup_service.py`
  `list_backups()`) returns `[{date: "YYYY-MM-DD", size_bytes}]` newest-first.
- Tests: `flutter_client/test/settings/backup_settings_admin_test.dart` covers
  the service (403 handling) and the card's admin gating; the fake service
  returns a single backup.

## Decisions

- **D1 — `DropdownButtonFormField<String>` keyed on the backup date, plus one
  Restore button beside it.** Same widget and decoration style as the
  security-question picker in `enable_encryption_screen.dart` (dense,
  outlined, `isExpanded: true`). Rules out a `DropdownMenu` (M3 menu with text
  entry — searching 30 dates is pointless) and a collapsible `ExpansionTile`
  (still 30 rows once opened, and it hides that backups exist at all).
- **D2 — Each item shows `date · size`** (e.g. `2026-10-14 · 12.3 MB`), the
  size formatted exactly as today. Keeps the size information admins have now;
  a second line per item is not needed.
- **D3 — The selection defaults to the newest backup** (first item; the API
  already sorts newest-first, and the client does not re-sort). The most
  common restore is "undo today's damage" → yesterday's backup is one click
  away. Rules out an empty "pick a backup" default, which adds a click for the
  common case and a disabled-button state.
- **D4 — Selection lives in a nullable `String? _selectedBackupDate`, resolved
  against the current list at build time**: if it is null or no longer in
  `_backups`, the newest date is used. The field is keyed on the list
  (`ValueKey` derived from the dates) so a reloaded list re-reads its initial
  value. This avoids Flutter's "value not in items" assertion and a stale
  selection after a reload; rules out storing the selection only inside the
  form field (lost on rebuild with a new list).
- **D5 — Restore flow unchanged.** The button calls the existing
  `_restoreBackup(selectedDate)`: same dialog text naming the date, same
  spinner, same reload / SnackBar. While a restore runs, both the drop-down and
  the button are disabled and the button is replaced by the spinner, as today.
- **D6 — Intro text, empty state and loading state unchanged.**
- **D7 — No API or service change.** Purely a client layout change.

## Review envelope

REVIEW.md defaults apply. The card is admin-only (E3: the admin is trusted);
the only input is the server's own backup list.

## Boundaries crossed

None. No endpoint, schema, stored data or export format changes. Older
clients keep the list layout and talk to the same API.

## Conventions

- Import nothing new; use Material widgets already in use in the client and
  the brand tokens via the existing theme (`design_tokens.dart` only if a new
  colour or spacing constant is needed — none is expected).

## Open decisions

None.

## Execution units

### Wave 1

#### U1 — Backups card drop-down

- **Goal:** Replace the per-backup rows in the Settings Backups card with one
  drop-down of backups (newest selected by default) and a single Restore
  button acting on the selection.
- **Scope:**
  - `flutter_client/lib/src/settings/settings_screen.dart`
  - `flutter_client/test/settings/backup_settings_admin_test.dart`
- **Context:** Read this plan's Current state and Decisions; read
  `settings_screen.dart` ~L100-110 (state), ~L497-555 (`_loadBackups`,
  `_restoreBackup`) and the Backups `_SectionCard` (~L1004-1062). **Example to
  follow:** the `DropdownButtonFormField<int>` in
  `flutter_client/lib/src/crypto/enable_encryption_screen.dart` (~L354) for
  widget, decoration and `ValueKey` use. Widget tests follow the existing
  `SettingsScreen Backups card` group in `backup_settings_admin_test.dart`.
- **Do:**
  1. Add `String? _selectedBackupDate;` next to the other backup state.
  2. Extract the inline size formatting into a private helper
     (`_backupSizeLabel(int bytes)`) with identical output.
  3. In the card's non-empty branch, keep the intro `Text` and replace
     `..._backups.map(...)` with a `Row`: an `Expanded`
     `DropdownButtonFormField<String>` (`isExpanded: true`, dense outlined
     decoration, `labelText: 'Backup'`, items `'$date · ${size}'` valued by
     date, `initialValue` = the resolved selection per D4, `key: ValueKey`
     derived from the joined dates, `onChanged` null while `_restoringDate !=
     null`, otherwise sets `_selectedBackupDate`), then a `SizedBox(width: 12)`,
     then either the existing 20×20 `CircularProgressIndicator` (when a restore
     is running) or a `TextButton('Restore')` calling
     `_restoreBackup(resolvedDate)`.
  4. Leave `_loadBackups`, `_restoreBackup`, the empty and loading branches
     untouched.
  5. Tests — make the fake service's backup list configurable (default one
     backup, so existing tests keep their meaning) and record restore calls;
     update the "Restore button per backup" test name/assertions to the new
     layout; add:
     - `shows one selector and one Restore button for several backups` — 3
       backups: exactly one `DropdownButtonFormField<String>`, exactly one
       Restore `TextButton`, only the newest date visible before opening.
     - `defaults to the newest backup and restores it` — tap Restore, the
       dialog names the newest date, confirm → the fake records that date.
     - `restores the backup picked in the drop-down` — open the drop-down,
       pick the second date, Restore, confirm → the fake records that date.
     - `size label is shown in the item` — e.g. `2048` bytes → `2 KB`,
       `5 * 1024 * 1024` → `5.0 MB` in the item text.
     For restore tests, override `restoreBackup` in the fake to record the
     date and throw an `Exception('stop')` so `reloadApp()` is not reached
     (the failure SnackBar path is fine).
- **Acceptance:**
  - `flutter test test/settings/backup_settings_admin_test.dart` passes with
    the four new tests and the updated existing ones (run in the
    `traxjourney-flutter-3.47.1-tests` container per the project's test
    setup).
  - `flutter analyze lib/src/settings/settings_screen.dart
    test/settings/backup_settings_admin_test.dart` reports no new issues.
  - Full `flutter test test/settings/` passes.
- **Out of scope:** any API or `SettingsService` change; re-sorting the list;
  showing backup times or relative ages; changing the restore dialog text;
  touching other Settings sections.
- **Latitude:** none
- **Escalate if:** a file outside Scope is needed (X3); the widget tests need
  a `pumpAndSettle` that deadlocks (see the settings-screen widget-test notes:
  pump narrowly instead) and narrowing does not resolve it; `reloadApp()`
  cannot be kept out of the restore tests by the throwing fake.
- **Depends on:** —
- **Route:** *(decided at delivery)*

## Definition of done

- An admin with N ≥ 1 backups sees one drop-down and one Restore button in the
  Backups card, regardless of N.
- The drop-down lists every backup as `date · size`, newest first, with the
  newest selected on load.
- Restore restores the selected date through the unchanged confirmation
  dialog; while it runs, the selector and button are disabled and a spinner
  shows.
- Non-admins still never see the card nor trigger a backup fetch; the empty and
  loading states read as before.
- The tests listed in U1 exist and pass; no API, schema or service change.
