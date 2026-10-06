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
import 'dart:math' as math;

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../core/project_ref.dart';
import '../track_metrics/elevation_gain.dart';
import '../track_metrics/elevation_profile.dart';
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
/// the downsampled profile (R3-1). The gain candidates keep their profile
/// envelope and stored gain as well, and the shared rows what tells whether
/// they may hold an envelope.
class CatchUpPayload {
  /// The trip's name as the server answered it.
  final String? name;

  /// The trip's `lock_version` the payload was built at, or null when the
  /// server did not send one (no pass can run without it).
  final int? lockVersion;

  /// The server's `caller_role`.
  final String? role;

  /// Activities with a non-empty `plain_fields`: id and the E2EE columns
  /// still holding plaintext. Never a shared row ([sharedActivities]).
  final List<({Object id, List<String> fields})> plainActivities;

  /// Activities a trip owned by someone else also holds
  /// (`shared_with_others`, decision 15; absent counts as not shared): they
  /// stay plaintext so that trip can read them, and are in no other list.
  /// [mayHoldEnvelope] is true when `/meta` cannot rule out an envelope in
  /// any of the row's E2EE columns — see [_mayHoldEnvelope] — and
  /// [signature] is what `/meta` showed of those columns, so a row found
  /// with nothing to decrypt is not read again until it changes.
  final List<({Object id, List<String> fields, bool mayHoldEnvelope, String signature})>
      sharedActivities;

  /// Activities whose stored gain the pass re-measures (decision 12): the
  /// legacy rows the pre-#374 sentinel and the unsmoothed gain could reach —
  /// GPX imports, and edits made before #386 (no gain snapshot; an absent
  /// `has_gain_snapshot` counts as none) — whose full profile is not
  /// plaintext. A later edit keeps its Strava-scaled gain and its profile,
  /// which the fixed pipeline wrote. [profile] is
  /// the payload's `elevation_profile_enc`: the low-res column's envelope in
  /// `/meta`, which is the full profile's (R4-5); null when that column is
  /// still plaintext, and the pass then reads it from `GET …/track`.
  final List<({Object id, String? profile, double? gain})> gainActivities;

  /// Memory and journal maps as received (shallow copies, so the in-place
  /// reveal of the originals does not reach them).
  final List<Map<String, dynamic>> memories;
  final List<Map<String, dynamic>> journals;

  const CatchUpPayload._(this.name, this.lockVersion, this.role,
      this.plainActivities, this.sharedActivities, this.gainActivities,
      this.memories, this.journals);

  factory CatchUpPayload.of(Map details) {
    final plainActivities = <({Object id, List<String> fields})>[];
    final sharedActivities = <({
      Object id,
      List<String> fields,
      bool mayHoldEnvelope,
      String signature
    })>[];
    final gainActivities = <({Object id, String? profile, double? gain})>[];
    for (final raw in (details['activities'] as List?) ?? const []) {
      final act = raw as Map;
      final id = act['id'];
      final fields = act['plain_fields'];
      if (id != null && act['shared_with_others'] == true) {
        final plain = fields is List ? fields.cast<String>() : const <String>[];
        sharedActivities.add((
          id: id as Object,
          fields: plain,
          mayHoldEnvelope: _mayHoldEnvelope(act, plain),
          signature: jsonEncode([
            plain,
            act['name'],
            act['start_latlng_enc'],
            act['end_latlng_enc'],
            act['elevation_profile_enc'],
            act['is_edited'],
          ]),
        ));
        continue;
      }
      if (id != null && fields is List && fields.isNotEmpty) {
        plainActivities.add((id: id as Object, fields: fields.cast<String>()));
      }
      // A full profile still in plaintext was measured by the server's own
      // pipeline (and repaired by its migration); the pass encrypts it below,
      // and the next pass re-measures it from the envelope.
      final profilePlain =
          fields is List && fields.contains('elevation_profile_json');
      final legacyEdit =
          act['is_edited'] == true && act['has_gain_snapshot'] != true;
      if (id != null &&
          !profilePlain &&
          (act['source'] == 'gpx' || legacyEdit)) {
        final enc = act['elevation_profile_enc'];
        gainActivities.add((
          id: id as Object,
          profile: enc is String ? enc : null,
          gain: (act['total_elevation_gain'] as num?)?.toDouble(),
        ));
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
      sharedActivities,
      gainActivities,
      memories,
      journals,
    );
  }

  /// Whether a `/meta` activity [act] may hold an envelope in an E2EE column,
  /// given its [plain] `plain_fields`. `/meta` shows the envelopes of the
  /// name (as is), the endpoints and the low-res profile (`*_enc`); for the
  /// polyline, the full profile and the four `original_*` snapshots it shows
  /// nothing, and a column `plain_fields` does not list is null **or** an
  /// envelope. So: true when a shown envelope exists or any column could be
  /// one — the snapshots only on an edited row, the only rows `GET …/track`
  /// returns them for. A row this cannot clear (no polyline, say) is read
  /// once per app session, then remembered by its signature.
  static bool _mayHoldEnvelope(Map act, List<String> plain) {
    final name = act['name'];
    if (name is String && EncryptedField.isEnvelope(name)) return true;
    if (act['start_latlng_enc'] != null ||
        act['end_latlng_enc'] != null ||
        act['elevation_profile_enc'] != null) {
      return true;
    }
    return encryptedFieldsByResource['activity']!.any((c) =>
        c != 'name' &&
        !plain.contains(c) &&
        (!c.startsWith('original_') || act['is_edited'] == true));
  }
}

/// What one pass, or the enable-time [EncryptionMigration.run], did.
class CatchUpResult {
  /// Rows written.
  int written = 0;

  /// Rows a write failed for (logged); the next load retries them.
  int skipped = 0;

  /// Activities that stay unencrypted, each counted once: rows the server
  /// would not let this user encrypt (404, decision 14: another traveller
  /// imported them), and rows a trip owned by someone else also holds
  /// (`shared_with_others`, or a 409 `shared_with_other_trip` when that trip
  /// took the row after the load; decision 15). Retrying does not help.
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
  ///   memories and journal entries are encrypted from the payload. An
  ///   activity a trip owned by someone else also holds is never encrypted,
  ///   and counted; its envelopes under the user's key are decrypted back
  ///   (decision 15, [_repairShared]).
  /// - Anyone else: the user's own plaintext journal entries are encrypted
  ///   (author key, decision 1), and every memory whose envelope decrypts
  ///   under the user's key is written back in plaintext — it was encrypted
  ///   under the wrong key (#505, decision 2).
  ///
  /// Then, as owner, the encrypted profile of every legacy GPX or edited
  /// activity is re-measured and its gain corrected (decision 12, see
  /// [CatchUpPayload.gainActivities] and [_recomputeGain]); never a shared
  /// one, which the server would refuse.
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
        final tripName = trip.name ?? ref.name;
        // What this pass read from GET …/track, and the rows the server
        // refused it (decision 14), for the gain recompute below.
        final tracks = <Object, Map<String, dynamic>>{};
        final refused = <Object>{};
        for (final act in trip.plainActivities) {
          _checkSession();
          final unencryptable = result.unencryptable;
          expected = await _encryptActivity(
              ref, tripName, act.id, act.fields, expected, result, tracks);
          if (result.unencryptable > unencryptable) refused.add(act.id);
        }
        for (final act in trip.sharedActivities) {
          _checkSession();
          result.unencryptable++;
          if (!act.mayHoldEnvelope) continue;
          expected = await _repairShared(ref, tripName, act.id, act.fields,
              act.signature, expected, result);
        }
        for (final act in trip.gainActivities) {
          if (refused.contains(act.id)) continue;
          _checkSession();
          expected = await _recomputeGain(tripName, act.id,
              tracks[act.id]?['elevation_profile_enc'] ?? act.profile, act.gain,
              expected, result);
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
  /// On an activity, a 404 means the user may not encrypt it (decision 14)
  /// and a 409 `shared_with_other_trip` that a trip owned by someone else
  /// took the row since the load (decision 15): the row is counted
  /// unencryptable, unless the pass [counted] it already.
  Future<int> _write(String path, Map<String, dynamic> body, int expected,
      CatchUpResult result, {bool activity = false, bool counted = false}) async {
    // The row's encryption awaited: the session may have changed meanwhile.
    _checkSession();
    final dynamic response;
    try {
      response = await _api.put(path, body);
    } on ApiException catch (e) {
      if (_isSessionRefusal(e)) throw const _SessionEnded();
      final conflict = _conflictCode(e);
      if (conflict == 'stale_write') throw const _TripChanged();
      if (activity &&
          (e.statusCode == 404 || conflict == 'shared_with_other_trip')) {
        if (!counted) result.unencryptable++;
        debugPrint('encryption catch-up: $path refused (${conflict ?? e.statusCode})');
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

  /// The `detail.code` of a 409, or null.
  static String? _conflictCode(ApiException e) {
    if (e.statusCode != 409) return null;
    try {
      final detail = (jsonDecode(e.body) as Map)['detail'];
      final code = detail is Map ? detail['code'] : null;
      return code is String ? code : null;
    } on Object {
      return null;
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
  /// never from the trip payload (R3-1). The answer is kept in [tracks].
  Future<int> _encryptActivity(
      ProjectRef ref,
      String tripName,
      Object id,
      List<String> fields,
      int expected,
      CatchUpResult result,
      Map<Object, Map<String, dynamic>> tracks) async {
    final track = await _readTrack(ref, id, expected, result);
    if (track == null) return expected;
    tracks[id] = track;

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

  /// One activity's `GET …/track`, or null — counted as skipped — when it
  /// could not be read. Ends the pass on a 401/403, and when the answer is
  /// at another lock version than [expected]: someone else wrote to the
  /// trip since the payload, and anything written from that payload
  /// (memories) could now be stale too (R4-1).
  Future<Map<String, dynamic>?> _readTrack(
      ProjectRef ref, Object id, int expected, CatchUpResult result) async {
    final Map<String, dynamic> track;
    try {
      track = await _api.get(ref.path('/activities/$id/track'))
          as Map<String, dynamic>;
    } on ApiException catch (e) {
      if (_isSessionRefusal(e)) throw const _SessionEnded();
      result.skipped++;
      debugPrint('encryption catch-up: activity $id not read (${e.statusCode})');
      return null;
    } on Exception catch (e) {
      result.skipped++;
      debugPrint('encryption catch-up: activity $id not read: $e');
      return null;
    }
    if (track['lock_version'] != expected) throw const _TripChanged();
    return track;
  }

  /// The shared rows found holding nothing this user can decrypt, by session
  /// user and activity id, with the `/meta` signature they were found at
  /// ([CatchUpPayload.sharedActivities]). Static for the reason
  /// [_converged] is: a row with another traveller's envelopes, or a column
  /// `/meta` cannot clear, would otherwise be read in full on every load. A
  /// shared row cannot gain an envelope while it stays shared (the server
  /// refuses it), and any change `/meta` shows changes the signature.
  static final Map<String, String> _sharedChecked = {};

  /// Write back in plaintext every E2EE field of a shared row (decision 15)
  /// that holds an envelope under this user's key: the shipped migration or
  /// an earlier pass encrypted it before another traveller's trip took it,
  /// and that trip cannot read it. All fields, the four `original_*`
  /// snapshots included, read from `GET …/track`. Another account's
  /// envelopes are left as they are. The row is already counted, so a
  /// refusal adds nothing to the count; other answers are [_write]'s.
  Future<int> _repairShared(ProjectRef ref, String tripName, Object id,
      List<String> plainFields, String signature, int expected,
      CatchUpResult result) async {
    final memoKey = '$_sessionUser|$id';
    if (_sharedChecked[memoKey] == signature) return expected;
    final track = await _readTrack(ref, id, expected, result);
    if (track == null) return expected;
    final body = await _plaintextBody(track, plainFields);
    if (body.isEmpty) {
      _sharedChecked[memoKey] = signature;
      return expected;
    }
    return _write('/api/activities/$id', {
      ...body,
      'project': tripName,
      'lock_version': expected,
    }, expected, result, activity: true, counted: true);
  }

  /// The plaintext of every E2EE field [track] holds as an envelope under
  /// this user's key, keyed by the `PUT /api/activities/{id}` body key.
  /// [plainFields] (the row's `plain_fields`) tells the profile columns
  /// apart: `/track` serves one profile envelope, the full one's when that
  /// column is an envelope, else the low-res one's. The low-res column gets
  /// the server's downsample of the decrypted profile ([_lowResProfile]).
  Future<Map<String, dynamic>> _plaintextBody(
      Map<String, dynamic> track, List<String> plainFields) async {
    final body = <String, dynamic>{};
    Future<void> decrypt(String key, Object? value) async {
      final plain = await _decryptOwn(value is String ? value : null);
      if (plain != null) body[key] = plain;
    }

    await decrypt('name', track['name']);
    await decrypt('summary_polyline', (track['map'] as Map?)?['summary_polyline']);
    await decrypt('start_latlng_json', track['start_latlng_enc']);
    await decrypt('end_latlng_json', track['end_latlng_enc']);
    final profileEnv = track['elevation_profile_enc'];
    final profile = await _decryptOwn(profileEnv is String ? profileEnv : null);
    if (profile != null) {
      if (!plainFields.contains('elevation_profile_json')) {
        body['elevation_profile_json'] = profile;
      }
      if (!plainFields.contains('elevation_profile_low_res_json')) {
        final lowRes = _lowResProfile(profile);
        if (lowRes != null) body['elevation_profile_low_res_json'] = lowRes;
      }
    }
    for (final field in encryptedFieldsByResource['activity']!) {
      if (field.startsWith('original_')) await decrypt(field, track[field]);
    }
    return body;
  }

  /// The low-res form the server stores beside a plaintext profile JSON
  /// (`_low_res_ep_json`, src/project/elevation_downsample.py): at most
  /// [_lowResPoints] points by Largest-Triangle-Three-Buckets, keeping the
  /// first, last, lowest and highest. Null when [profileJson] does not parse.
  static String? _lowResProfile(String profileJson) {
    try {
      final ep = jsonDecode(profileJson) as Map;
      final d = [for (final v in ep['distances_km'] as List) (v as num).toDouble()];
      final e = [for (final v in ep['elevations_m'] as List) (v as num).toDouble()];
      final n = math.min(d.length, e.length);
      if (n == 0) return null;
      var keep = List<int>.generate(n, (i) => i);
      if (n > _lowResPoints) {
        final idx = _lttbIndices(d, e, n, _lowResPoints).toSet();
        var lo = 0, hi = 0;
        for (var i = 1; i < n; i++) {
          if (e[i] < e[lo]) lo = i;
          if (e[i] > e[hi]) hi = i;
        }
        keep = (idx..add(lo)..add(hi)).toList()..sort();
      }
      return jsonEncode({
        'distances_km': [for (final i in keep) d[i]],
        'elevations_m': [for (final i in keep) e[i]],
      });
    } on Object {
      return null;
    }
  }

  /// `DEFAULT_MAX_POINTS` of src/project/elevation_downsample.py.
  static const _lowResPoints = 300;

  /// `_lttb_indices` of src/project/elevation_downsample.py.
  static List<int> _lttbIndices(
      List<double> xs, List<double> ys, int n, int threshold) {
    final out = [0];
    var a = 0;
    final every = (n - 2) / (threshold - 2);
    for (var i = 0; i < threshold - 2; i++) {
      // Centroid of the next bucket.
      final ns = ((i + 1) * every).floor() + 1;
      final ne = math.min(((i + 2) * every).floor() + 1, n);
      final cnt = math.max(ne - ns, 1);
      var sumX = 0.0, sumY = 0.0;
      for (var j = ns; j < ne; j++) {
        sumX += xs[j];
        sumY += ys[j];
      }
      final avgX = sumX / cnt, avgY = sumY / cnt;
      // The current bucket's point making the largest triangle with the
      // last one kept and that centroid.
      final cs = (i * every).floor() + 1;
      final ce = math.min(((i + 1) * every).floor() + 1, n);
      final ax = xs[a], ay = ys[a];
      var maxArea = -1.0;
      var best = cs;
      for (var j = cs; j < ce; j++) {
        final area =
            ((ax - avgX) * (ys[j] - ay) - (ax - xs[j]) * (avgY - ay)).abs();
        if (area > maxArea) {
          maxArea = area;
          best = j;
        }
      }
      out.add(best);
      a = best;
    }
    out.add(n - 1);
    return out;
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

  /// The rows [_recomputeGain] found converged — measured gain within 0.5 m
  /// of the stored one, nothing to repair — by session user and activity id,
  /// with the profile envelope and stored gain they were found at (U8-R1-1).
  /// Static because an [EncryptionMigration] is built per pass: without it
  /// every load would decrypt and re-measure every legacy profile again. A
  /// row whose envelope or gain has changed since (an edit, a repair,
  /// another device) no longer matches and is measured anew; one entry per
  /// row, so it holds at most one envelope per activity.
  static final Map<String, ({String envelope, double? gain})> _converged = {};

  /// Forgets every converged row and every checked shared row, as a new app
  /// session would.
  @visibleForTesting
  static void resetConvergedForTest() {
    _converged.clear();
    _sharedChecked.clear();
  }

  /// Re-measure one encrypted activity's elevation gain from its full
  /// [envelope] with the `track_metrics/` port (decision 12, #366): the
  /// server cannot read the profile to do it, so legacy encrypted rows still
  /// carry the gain of the pre-#374 sentinel and the unsmoothed measure.
  ///
  /// A profile still holding the 0.0 dropout sentinel is repaired as
  /// migration c4a9e1f70b38 repairs plaintext ones and written back, one
  /// envelope to both profile columns (R4-5). The gain is written only when it
  /// differs from the [stored] one by more than 0.5 m, so a second pass writes
  /// nothing. Both writes carry the pass's lock version. A row found
  /// converged earlier in this app session, at the same envelope and stored
  /// gain, is not decrypted again.
  Future<int> _recomputeGain(String tripName, Object id, Object? envelope,
      double? stored, int expected, CatchUpResult result) async {
    if (envelope is! String || !EncryptedField.isEnvelope(envelope)) {
      return expected;
    }
    final memoKey = '$_sessionUser|$id';
    if (_converged[memoKey] == (envelope: envelope, gain: stored)) {
      return expected;
    }
    var current = envelope;
    final profile = await _decryptProfile(id, envelope);
    if (profile == null) return expected;
    final distances = profile.distances;
    var elevations = profile.elevations;

    final mask = sentinelMask(elevations, distances);
    if (mask.contains(true)) {
      elevations = interpolateElevationGaps(distances, [
        for (var i = 0; i < elevations.length; i++) mask[i] ? null : elevations[i],
      ]);
      final repaired = await _enc.encryptText(jsonEncode(
          {'distances_km': distances, 'elevations_m': elevations}));
      final written = result.written;
      expected = await _write('/api/activities/$id', {
        'elevation_profile_json': repaired,
        'elevation_profile_low_res_json': repaired,
        'project': tripName,
        'lock_version': expected,
      }, expected, result, activity: true);
      // The stored profile is still the sentinel one: its gain would not match.
      if (result.written == written) return expected;
      current = repaired;
    }

    final gain = elevationGain(elevations, distances);
    if (stored != null && (gain - stored).abs() <= 0.5) {
      _converged[memoKey] = (envelope: current, gain: stored);
      return expected;
    }
    final written = result.written;
    expected = await _write('/api/activities/$id/elevation-gain', {
      'total_elevation_gain': gain,
      'project': tripName,
      'lock_version': expected,
    }, expected, result, activity: true);
    if (result.written > written) {
      _converged[memoKey] = (envelope: current, gain: gain);
    }
    return expected;
  }

  /// The series of a profile [envelope], or null — logged — when it is not
  /// under this user's key (another traveller's row) or not a profile of at
  /// least two samples, which is what the server's repair skips too.
  Future<({List<double> distances, List<double> elevations})?> _decryptProfile(
      Object id, String envelope) async {
    try {
      final ep = jsonDecode(await _enc.decryptText(envelope)) as Map;
      final distances = [
        for (final v in ep['distances_km'] as List) (v as num).toDouble()
      ];
      final elevations = [
        for (final v in ep['elevations_m'] as List) (v as num).toDouble()
      ];
      if (elevations.length < 2 || distances.length != elevations.length) {
        debugPrint('encryption catch-up: activity $id: profile too short');
        return null;
      }
      return (distances: distances, elevations: elevations);
    } catch (e) {
      debugPrint('encryption catch-up: activity $id: profile not readable: $e');
      return null;
    }
  }
}
