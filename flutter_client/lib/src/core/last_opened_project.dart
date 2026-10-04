/// Shared helpers for remembering the last project a user opened (issue #93),
/// so the bare-root route (`/`) can bypass `/projects` and drop the user
/// straight back into it. Scoped per-user (by [userId]) so a shared
/// browser/device doesn't leak user A's last trip into user B's session.
library;

import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'project_ref.dart';

String _prefKey(String userId) => 'last_opened_project_$userId';

/// Null for no account, and for an account whose id is not known: an empty
/// id used to be every restored session's (issue #418), and a key with no
/// id in it is a key every account on the device shares.
String? _keyFor(String? userId) =>
    (userId == null || userId.isEmpty) ? null : _prefKey(userId);

/// Removes the key that every restored session shared before user ids were
/// real (issue #418). It can hold any account's last trip, so nobody may
/// read it, and nothing writes it any more. Run at startup, before the first
/// redirect could read it; removing a key that is not there costs nothing,
/// so it simply runs on every start.
Future<void> purgeSharedLastOpenedProject() async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.remove(_prefKey(''));
}

/// Records [ref] as [userId]'s last successfully opened project (name +
/// owner — issue #106, so a shared project's owner survives a reload).
/// No-op if [userId] is null or empty (not logged in / id not known).
Future<void> saveLastOpenedProject(String? userId, ProjectRef ref) async {
  final key = _keyFor(userId);
  if (key == null) return;
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(key, jsonEncode(ref.toJson()));
}

/// Forgets [userId]'s last-opened project — e.g. after leaving a shared trip
/// (issue #106), so the bare-root redirect doesn't drop the user back into a
/// project they no longer have access to. No-op if [userId] is null or empty.
Future<void> clearLastOpenedProject(String? userId) async {
  final key = _keyFor(userId);
  if (key == null) return;
  final prefs = await SharedPreferences.getInstance();
  await prefs.remove(key);
}

/// Reads [userId]'s last-opened project ref, or null if none is recorded.
/// No-op (returns null) if [userId] is null or empty. Backward compatible with the
/// pre-#106 format, which stored the bare project name as a plain string.
Future<ProjectRef?> readLastOpenedProject(String? userId) async {
  final key = _keyFor(userId);
  if (key == null) return null;
  final prefs = await SharedPreferences.getInstance();
  final raw = prefs.getString(key);
  if (raw == null || raw.isEmpty) return null;
  try {
    final decoded = jsonDecode(raw);
    if (decoded is Map) {
      return ProjectRef.fromJson(decoded.cast<String, dynamic>());
    }
  } catch (_) {
    // Not JSON — pre-#106 plain project-name string.
  }
  return ProjectRef(name: raw);
}

/// Resolves where the bare-root route (`/`) should send a logged-in user:
/// their last-opened project (`/view?project=<name>[&owner=<id>]`) if one is
/// recorded for [userId], otherwise `/projects`.
Future<String> rootRedirectTarget(String? userId) async {
  final lastRef = await readLastOpenedProject(userId);
  if (lastRef != null && lastRef.name.isNotEmpty) {
    return lastRef.withOwner('/view?project=${Uri.encodeComponent(lastRef.name)}');
  }
  return '/projects';
}
