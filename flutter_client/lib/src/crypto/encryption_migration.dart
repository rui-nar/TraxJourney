/// Encryption of a user's still-plaintext content: once when they enable
/// encryption (#26 memory/journal text, #29 activity geometry), and again
/// after every trip load (the catch-up, E2EE remnants decision 6), for content
/// that arrived in plaintext since — Strava, GPX, split and `.traxj` imports,
/// legacy Polarsteps memories.
///
/// Client-side by necessity (the server can't read the plaintext to encrypt it).
/// Idempotent and resumable: it re-encrypts only fields that are still plaintext
/// (an already-encrypted envelope is left untouched), so a re-run finishes the
/// job after any interruption and never double-encrypts. Every write is a
/// compare-and-swap on the trip's lock version (decision 13): a pass working
/// from a payload someone else has since changed stops, and the next load
/// retries from fresh data.
library;

import 'dart:convert';

import 'package:flutter/foundation.dart';

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
    // The edit snapshots Reset restores from: encrypted, never nulled (R1-1).
    'original_polyline',
    'original_elevation_profile_json',
    'original_start_latlng_json',
    'original_end_latlng_json',
  },
};

/// The part of a trip payload a pass reads, copied **before** the load
/// decrypts the payload in place: fed the revealed maps, the pass would take
/// every envelope for plaintext and re-encrypt the whole trip on every load,
/// and could not tell which memories were envelopes (R1-9).
///
/// Activities keep only their id and `plain_fields`: their geometry is never
/// encrypted from a trip payload, whose `/meta` form has no polyline and only
/// the downsampled profile (R3-1).
class CatchUpPayload {
  /// The trip's name as the server answered it.
  final String? name;

  /// The trip's `lock_version` the payload was built at, or null when the
  /// server did not send one (no pass can run without it).
  final int? lockVersion;

  /// The server's `caller_role`.
  final String? role;

  /// Activities with a non-empty `plain_fields`: id and the E2EE columns
  /// still holding plaintext.
  final List<({Object id, List<String> fields})> plainActivities;

  /// Memory and journal maps as received (shallow copies, so the in-place
  /// reveal of the originals does not reach them).
  final List<Map<String, dynamic>> memories;
  final List<Map<String, dynamic>> journals;

  const CatchUpPayload._(this.name, this.lockVersion, this.role,
      this.plainActivities, this.memories, this.journals);

  factory CatchUpPayload.of(Map details) {
    final plainActivities = <({Object id, List<String> fields})>[];
    for (final raw in (details['activities'] as List?) ?? const []) {
      final act = raw as Map;
      final id = act['id'];
      final fields = act['plain_fields'];
      if (id != null && fields is List && fields.isNotEmpty) {
        plainActivities.add((id: id as Object, fields: fields.cast<String>()));
      }
    }
    final memories = <Map<String, dynamic>>[];
    final journals = <Map<String, dynamic>>[];
    for (final raw in (details['items'] as List?) ?? const []) {
      final item = raw as Map;
      switch (item['item_type']) {
        case 'memory':
          final m = item['memory'];
          if (m is Map) memories.add(Map<String, dynamic>.from(m));
        case 'journal':
          final j = item['journal'];
          if (j is Map) journals.add(Map<String, dynamic>.from(j));
      }
    }
    return CatchUpPayload._(
      details['name'] as String?,
      details['lock_version'] as int?,
      details['caller_role'] as String?,
      plainActivities,
      memories,
      journals,
    );
  }
}

/// What one pass, or the enable-time [EncryptionMigration.run], did.
class CatchUpResult {
  /// Rows written.
  int written = 0;

  /// Rows a write failed for (logged); the next load retries them.
  int skipped = 0;

  /// Activities the server would not let this user encrypt (404, decision
  /// 14): rows another traveller imported. Retrying does not help.
  int unencryptable = 0;

  /// Passes that ended early because the trip changed under them (a
  /// `stale_write`, or a `GET …/track` at another lock version).
  int ended = 0;

  /// The pass ended because the session it ran for is gone: signed out,
  /// locked, another account signed in, or the server refused the session
  /// (401/403). Whatever it found is not this trip's answer (U7-R1-1).
  bool sessionEnded = false;

  bool get complete => skipped == 0 && unencryptable == 0 && ended == 0;

  void _add(CatchUpResult other) {
    written += other.written;
    skipped += other.skipped;
    unencryptable += other.unencryptable;
    ended += other.ended;
  }
}

/// Thrown inside a pass to end it: the trip changed since its payload.
class _TripChanged implements Exception {
  const _TripChanged();
}

/// Thrown inside a pass to end it: the session it started in is over.
class _SessionEnded implements Exception {
  const _SessionEnded();
}

class EncryptionMigration {
  final ApiClient _api;
  final EncryptionService _enc;
  EncryptionMigration(this._api, this._enc);

  /// Encrypt all still-plaintext memory/journal text and activity geometry
  /// across every trip the user owns, one [encryptTrip] each. Trips shared
  /// with the user are left to the catch-up on their next load. No-op if
  /// encryption isn't unlocked.
  Future<CatchUpResult> run() async {
    final total = CatchUpResult();
    if (!_enc.isUnlocked) return total;
    final projects = await _api.get('/api/projects/') as List;
    for (final p in projects) {
      final entry = (p as Map).cast<String, dynamic>();
      // Issue #106: the list includes projects shared *with* this user. Their
      // memories belong under the owner's key, so only the user's own trips
      // are migrated here; the catch-up handles their journal entries on
      // a shared trip when it loads.
      if (entry.isSharedWithMe) continue;
      final ref = entry.ref;
      try {
        final details = await _api.get(ref.path('/meta')) as Map<String, dynamic>;
        final trip = CatchUpPayload.of(details);
        final lockVersion = trip.lockVersion;
        if (lockVersion == null) continue;
        final result = await encryptTrip(ref, trip,
            role: trip.role ?? 'owner', lockVersion: lockVersion);
        total._add(result);
        if (result.sessionEnded) {
          total.sessionEnded = true;
          break;
        }
      } on Exception catch (e) {
        debugPrint('encryption migration: trip ${ref.name} not loaded: $e');
        total.skipped++;
      }
    }
    return total;
  }

  /// One trip's pass, from its pre-reveal [trip] payload, the caller's [role]
  /// on it and the [lockVersion] that payload was built at.
  ///
  /// - Owner: every activity listing `plain_fields` is re-read from
  ///   `GET …/track` and its listed fields encrypted from that; plaintext
  ///   memories and journal entries are encrypted from the payload.
  /// - Anyone else: the user's own plaintext journal entries are encrypted
  ///   (author key, decision 1), and every memory whose envelope decrypts
  ///   under the user's key is written back in plaintext — it was encrypted
  ///   under the wrong key (#505, decision 2).
  ///
  /// The pass holds one expected lock version, advanced only by its own
  /// writes (R4-1). It ends at a `stale_write` or at a `GET …/track` answered
  /// at another lock version; any other failed write skips that row only.
  ///
  /// It also ends as soon as the session it started in is over (U7-R1-1): a
  /// 401/403, or — checked before every row and every write — the key
  /// locked or another account's token in [ApiClient]. Otherwise a sign-out
  /// followed by another sign-in on the same device would encrypt the rest
  /// of this trip under the other account's key, with its token.
  Future<CatchUpResult> encryptTrip(
    ProjectRef ref,
    CatchUpPayload trip, {
    required String role,
    required int lockVersion,
  }) async {
    final result = CatchUpResult();
    if (!_enc.isUnlocked || role == 'viewer') return result;
    _sessionUser = _api.tokenUserId;
    var expected = lockVersion;
    try {
      if (role == 'owner') {
        for (final act in trip.plainActivities) {
          _checkSession();
          expected = await _encryptActivity(
              ref, trip.name ?? ref.name, act.id, act.fields, expected, result);
        }
        for (final mem in trip.memories) {
          _checkSession();
          expected = await _encryptMemory(mem, expected, result);
        }
      } else {
        for (final mem in trip.memories) {
          _checkSession();
          expected = await _restoreMemory(mem, expected, result);
        }
      }
      for (final j in trip.journals) {
        _checkSession();
        expected = await _encryptJournal(j, expected, result);
      }
    } on _TripChanged {
      result.ended++;
    } on _SessionEnded {
      result.ended++;
      result.sessionEnded = true;
    }
    return result;
  }

  /// The account [encryptTrip] started for, by the token's user id.
  int? _sessionUser;

  void _checkSession() {
    if (!_enc.isUnlocked || _api.tokenUserId != _sessionUser) {
      throw const _SessionEnded();
    }
  }

  static bool _isSessionRefusal(ApiException e) =>
      e.statusCode == 401 || e.statusCode == 403;

  bool _isPlain(String? v) =>
      v != null && v.isNotEmpty && !EncryptedField.isEnvelope(v);

  /// Encrypt only if the value is still plaintext; leave envelopes/nulls as-is.
  Future<String?> _protectIfPlain(String? v) async =>
      _isPlain(v) ? await _enc.protect(v) : v;

  /// The plaintext of [v] when it is an envelope under this user's key, else
  /// null (plaintext, or another account's envelope).
  Future<String?> _decryptOwn(String? v) async {
    if (v == null || !EncryptedField.isEnvelope(v)) return null;
    try {
      return await _enc.decryptText(v);
    } catch (_) {
      return null;
    }
  }

  /// PUT [body] with the pass's [expected] lock version; returns the next
  /// one. A `stale_write` ends the pass; any other failure skips this row.
  /// A 404 on an activity means the user may not encrypt it (decision 14).
  Future<int> _write(String path, Map<String, dynamic> body, int expected,
      CatchUpResult result, {bool activity = false}) async {
    // The row's encryption awaited: the session may have changed meanwhile.
    _checkSession();
    final dynamic response;
    try {
      response = await _api.put(path, body);
    } on ApiException catch (e) {
      if (_isSessionRefusal(e)) throw const _SessionEnded();
      if (_isStaleWrite(e)) throw const _TripChanged();
      if (activity && e.statusCode == 404) {
        result.unencryptable++;
      } else {
        result.skipped++;
        debugPrint('encryption catch-up: $path refused (${e.statusCode})');
      }
      return expected;
    } on Exception catch (e) {
      result.skipped++;
      debugPrint('encryption catch-up: $path failed: $e');
      return expected;
    }
    result.written++;
    return (response as Map)['lock_version'] as int;
  }

  static bool _isStaleWrite(ApiException e) {
    if (e.statusCode != 409) return false;
    try {
      final detail = (jsonDecode(e.body) as Map)['detail'];
      return detail is Map && detail['code'] == 'stale_write';
    } on Object {
      return false;
    }
  }

  Future<int> _encryptMemory(
      Map<String, dynamic> mem, int expected, CatchUpResult result) async {
    final name = mem['name'] as String?;
    final desc = mem['description'] as String?;
    if (!_isPlain(name) && !_isPlain(desc)) return expected;
    return _write('/api/memories/${mem['id']}', {
      ..._memoryBody(mem),
      if (name != null) 'name': await _protectIfPlain(name),
      if (desc != null) 'description': await _protectIfPlain(desc),
      'lock_version': expected,
    }, expected, result);
  }

  Future<int> _restoreMemory(
      Map<String, dynamic> mem, int expected, CatchUpResult result) async {
    final name = mem['name'] as String?;
    final desc = mem['description'] as String?;
    final plainName = await _decryptOwn(name);
    final plainDesc = await _decryptOwn(desc);
    if (plainName == null && plainDesc == null) return expected;
    return _write('/api/memories/${mem['id']}', {
      ..._memoryBody(mem),
      if (name != null) 'name': plainName ?? name,
      if (desc != null) 'description': plainDesc ?? desc,
      'lock_version': expected,
    }, expected, result);
  }

  /// A memory's non-encrypted fields, re-sent as they are.
  Map<String, dynamic> _memoryBody(Map<String, dynamic> mem) => {
        'date': mem['date'],
        'geo_mode': mem['geo_mode'] ?? 'start_of_day',
        if (mem['time'] != null) 'time': mem['time'],
        if (mem['lat'] != null) 'lat': mem['lat'],
        if (mem['lon'] != null) 'lon': mem['lon'],
      };

  Future<int> _encryptJournal(
      Map<String, dynamic> j, int expected, CatchUpResult result) async {
    final desc = j['description'] as String?;
    if (!_isPlain(desc)) return expected;
    return _write('/api/journal/${j['id']}', {
      'date': j['date'],
      'geo_mode': j['geo_mode'] ?? 'start_of_day',
      if (j['time'] != null) 'time': j['time'],
      'description': await _enc.protect(desc),
      if (j['lat'] != null) 'lat': j['lat'],
      if (j['lon'] != null) 'lon': j['lon'],
      'lock_version': expected,
    }, expected, result);
  }

  /// Encrypt one activity's listed plaintext [fields], read from
  /// `GET …/track` — the full track and profile and the edit snapshots — and
  /// never from the trip payload (R3-1).
  Future<int> _encryptActivity(ProjectRef ref, String tripName, Object id,
      List<String> fields, int expected, CatchUpResult result) async {
    final Map<String, dynamic> track;
    try {
      track = await _api.get(ref.path('/activities/$id/track'))
          as Map<String, dynamic>;
    } on ApiException catch (e) {
      if (_isSessionRefusal(e)) throw const _SessionEnded();
      result.skipped++;
      debugPrint('encryption catch-up: activity $id not read (${e.statusCode})');
      return expected;
    } on Exception catch (e) {
      result.skipped++;
      debugPrint('encryption catch-up: activity $id not read: $e');
      return expected;
    }
    // Someone else wrote to the trip since the payload: anything encrypted
    // from that payload (memories) could now be stale too (R4-1).
    if (track['lock_version'] != expected) throw const _TripChanged();

    final body = await _activityBody(track, fields);
    final missing = fields.where((f) => !body.containsKey(f)).toList();
    if (missing.isNotEmpty) {
      debugPrint('encryption catch-up: activity $id: $missing not in /track');
    }
    if (body.isEmpty) {
      result.skipped++;
      return expected;
    }
    return _write('/api/activities/$id', {
      ...body,
      'project': tripName,
      'lock_version': expected,
    }, expected, result, activity: true);
  }

  /// Envelopes for each of [fields] that [track] holds in plaintext, keyed by
  /// the `PUT /api/activities/{id}` body key (the column name).
  Future<Map<String, dynamic>> _activityBody(
      Map<String, dynamic> track, List<String> fields) async {
    final body = <String, dynamic>{};
    Future<void> encrypt(String key, String? plain) async {
      if (_isPlain(plain)) body[key] = await _enc.protect(plain);
    }

    // Column order, not list order: the low-res profile depends on the full.
    for (final field in encryptedFieldsByResource['activity']!) {
      if (!fields.contains(field) || body.containsKey(field)) continue;
      switch (field) {
        case 'name':
          await encrypt(field, track['name'] as String?);
        case 'summary_polyline':
          await encrypt(field, (track['map'] as Map?)?['summary_polyline'] as String?);
        case 'start_latlng_json':
          final v = track['start_latlng'];
          if (v is List) await encrypt(field, jsonEncode(v));
        case 'end_latlng_json':
          final v = track['end_latlng'];
          if (v is List) await encrypt(field, jsonEncode(v));
        case 'elevation_profile_json':
          // The full profile, one envelope written to both profile columns:
          // the low-res copy cannot be derived from ciphertext later, and the
          // chart downsamples on the device (R4-5).
          final env = await _profileEnvelope(track['elevation_profile']);
          if (env != null) {
            body['elevation_profile_json'] = env;
            body['elevation_profile_low_res_json'] = env;
          }
        case 'elevation_profile_low_res_json':
          // Only the low-res copy is plaintext. When the full profile is
          // already an envelope, that envelope is the low-res copy too;
          // otherwise the row has no full profile and /track's profile is the
          // low-res one.
          final fullEnv = track['elevation_profile_enc'];
          if (fullEnv is String && EncryptedField.isEnvelope(fullEnv)) {
            body[field] = fullEnv;
          } else {
            final env = await _profileEnvelope(track['elevation_profile']);
            if (env != null) body[field] = env;
          }
        default:
          // The four original_* snapshots, as stored.
          await encrypt(field, track[field] as String?);
      }
    }
    return body;
  }

  /// The stored profile JSON for /track's `[[dist_km, elev_m], …]` pairs,
  /// encrypted; null when there are none.
  Future<String?> _profileEnvelope(Object? pairs) async {
    if (pairs is! List) return null;
    final epJson = jsonEncode({
      'distances_km': [for (final p in pairs) (p as List)[0]],
      'elevations_m': [for (final p in pairs) (p as List)[1]],
    });
    return _enc.protect(epJson);
  }
}
