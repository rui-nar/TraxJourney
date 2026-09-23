/// One-time migration of a user's EXISTING plaintext memory/journal text
/// (#26, Phase 7) and activity geometry (#29) to ciphertext when they enable
/// encryption.
///
/// Client-side by necessity (the server can't read the plaintext to encrypt it).
/// Idempotent and resumable: it re-encrypts only fields that are still plaintext
/// (an already-encrypted envelope is left untouched), so a re-run finishes the
/// job after any interruption and never double-encrypts.
library;

import 'dart:convert';

import '../api/client.dart';
import '../core/project_ref.dart';
import 'e2ee_crypto.dart';
import 'encryption_service.dart';

/// Every field the client end-to-end encrypts, per API resource, named as the
/// PUT/POST body key it travels under (issue #433).
///
/// This is the whole list. The create/update paths
/// (projects/project_memory_crud_mixin.dart, projects/project_journal_crud_mixin.dart)
/// protect the same memory/journal fields, and activities are encrypted only
/// by [EncryptionMigration]. docs/ENCRYPTION.md documents coverage from this
/// list and test/crypto/encryption_coverage_test.dart asserts the migration's
/// writes match it exactly, so a field added to one must be added to all three.
const encryptedFieldsByResource = <String, Set<String>>{
  'memory': {'name', 'description'},
  'journal': {'description'},
  'activity': {
    'name',
    'summary_polyline',
    'start_latlng_json',
    'end_latlng_json',
    'elevation_profile_json',
    'elevation_profile_low_res_json',
  },
};

class EncryptionMigration {
  final ApiClient _api;
  final EncryptionService _enc;
  EncryptionMigration(this._api, this._enc);

  /// Encrypt all still-plaintext memory/journal text and activity geometry
  /// (name, summary_polyline, start/end latlng, elevation profile — issue #29)
  /// across every project. Returns the number of rows updated. No-op if
  /// encryption isn't unlocked.
  Future<int> run() async {
    if (!_enc.isUnlocked) return 0;
    var migrated = 0;
    final projects = await _api.get('/api/projects/') as List;
    for (final p in projects) {
      final entry = (p as Map).cast<String, dynamic>();
      // Issue #106: the list can now include projects shared *with* this
      // user (role "editor") alongside their own. Migration re-encrypts the
      // caller's own content under the caller's own key — key-sharing with
      // companions on someone else's project is explicitly out of scope
      // (see the #106 plan), so only the caller's own projects are migrated.
      if (entry.isSharedWithMe) continue;
      final ref = entry.ref;
      final details = await _api.get(ref.path()) as Map<String, dynamic>;
      final items = (details['items'] as List?) ?? const [];
      for (final raw in items) {
        final item = raw as Map;
        switch (item['item_type']) {
          case 'memory':
            migrated += await _migrateMemory(item['memory'] as Map?);
          case 'journal':
            migrated += await _migrateJournal(item['journal'] as Map?);
        }
      }
      final activities = (details['activities'] as List?) ?? const [];
      for (final raw in activities) {
        migrated += await _migrateActivity(raw as Map?);
      }
    }
    return migrated;
  }

  bool _isPlain(String? v) =>
      v != null && v.isNotEmpty && !EncryptedField.isEnvelope(v);

  /// Encrypt only if the value is still plaintext; leave envelopes/nulls as-is.
  Future<String?> _protectIfPlain(String? v) async =>
      _isPlain(v) ? await _enc.protect(v) : v;

  Future<int> _migrateMemory(Map? mem) async {
    if (mem == null) return 0;
    final name = mem['name'] as String?;
    final desc = mem['description'] as String?;
    if (!_isPlain(name) && !_isPlain(desc)) return 0;
    await _api.put('/api/memories/${mem['id']}', {
      'date': mem['date'],
      'geo_mode': mem['geo_mode'] ?? 'start_of_day',
      if (name != null) 'name': await _protectIfPlain(name),
      if (mem['time'] != null) 'time': mem['time'],
      if (desc != null) 'description': await _protectIfPlain(desc),
      if (mem['lat'] != null) 'lat': mem['lat'],
      if (mem['lon'] != null) 'lon': mem['lon'],
    });
    return 1;
  }

  Future<int> _migrateJournal(Map? j) async {
    if (j == null) return 0;
    final desc = j['description'] as String?;
    if (!_isPlain(desc)) return 0;
    await _api.put('/api/journal/${j['id']}', {
      'date': j['date'],
      'geo_mode': j['geo_mode'] ?? 'start_of_day',
      if (j['time'] != null) 'time': j['time'],
      if (desc != null) 'description': await _protectIfPlain(desc),
      if (j['lat'] != null) 'lat': j['lat'],
      if (j['lon'] != null) 'lon': j['lon'],
    });
    return 1;
  }

  /// Migrate one activity's E2EE-in-scope fields (issue #29): name,
  /// summary_polyline, start/end latlng, elevation profile. Idempotent by the
  /// same structural detection as memories/journal — for name/summary_polyline
  /// that's `_isPlain`; for start_latlng/end_latlng/elevation_profile it's
  /// implicit, because the server itself nulls those parsed fields out once
  /// the underlying column is ciphertext (see ActivityMixin._row_to_activity),
  /// so a re-run naturally sees nothing left to migrate.
  Future<int> _migrateActivity(Map? act) async {
    if (act == null) return 0;
    final id = act['id'];
    if (id == null) return 0;

    final name = act['name'] as String?;
    // The wire shape nests the polyline under "map" (to_strava_dict()'s
    // {"map": {"summary_polyline": ...}}).
    final summaryPolyline = (act['map'] as Map?)?['summary_polyline'] as String?;
    final startLatlng = act['start_latlng'];
    final endLatlng = act['end_latlng'];
    final elevationProfile = act['elevation_profile']; // [[dist_km, elev_m], …]

    final body = <String, dynamic>{};
    if (_isPlain(name)) {
      body['name'] = await _enc.protect(name);
    }
    if (_isPlain(summaryPolyline)) {
      body['summary_polyline'] = await _enc.protect(summaryPolyline);
    }
    if (startLatlng is List && startLatlng.isNotEmpty) {
      body['start_latlng_json'] = await _enc.protect(jsonEncode(startLatlng));
    }
    if (endLatlng is List && endLatlng.isNotEmpty) {
      body['end_latlng_json'] = await _enc.protect(jsonEncode(endLatlng));
    }
    if (elevationProfile is List && elevationProfile.isNotEmpty) {
      final distances = [for (final pair in elevationProfile) (pair as List)[0]];
      final elevations = [for (final pair in elevationProfile) (pair as List)[1]];
      final epJson = jsonEncode({'distances_km': distances, 'elevations_m': elevations});
      final encEp = await _enc.protect(epJson);
      body['elevation_profile_json'] = encEp;
      // The API never exposes the true downsampled low-res series separately
      // (it's folded into `elevation_profile` as a meta/low-res-load fallback
      // only) — there's nothing distinct for the client to read and encrypt.
      // Re-encrypting the full series here avoids leaving
      // elevation_profile_low_res_json as a plaintext remnant; the elevation
      // chart re-downsamples client-side regardless (_kMaxChartPoints), so
      // this doesn't change what's rendered, only a slightly larger payload
      // for meta/low-res-only loads.
      body['elevation_profile_low_res_json'] = encEp;
    }
    if (body.isEmpty) return 0;

    // original_polyline / original_elevation_profile_json (issue #31's
    // edit-undo snapshot) are DB-only and never surfaced to the client via any
    // GET endpoint, so they can't be read-then-encrypted here. Best-effort
    // scrub them to null instead whenever an activity is migrated: they become
    // unreachable dead weight once track editing is disabled for encrypted
    // activities, and clearing them avoids leaving plaintext GPS remnants
    // around without a new endpoint to expose them for encryption.
    body['original_polyline'] = null;
    body['original_elevation_profile_json'] = null;

    await _api.put('/api/activities/$id', body);
    return 1;
  }
}
