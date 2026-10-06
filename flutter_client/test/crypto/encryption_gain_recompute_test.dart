/// The catch-up's elevation-gain recompute for encrypted activities (E2EE
/// remnants decision 12, issue #366): the server cannot read an encrypted
/// profile, so the pass of decision 6 measures it on the device with the
/// `track_metrics/` port, repairs a pre-#374 dropout sentinel, and writes the
/// gain back when it is more than 0.5 m off.
///
/// Every test runs [EncryptionMigration.encryptTrip] against [_Server], which
/// stores rows the way the database does and builds `/meta`, `GET …/track`
/// and the write answers from them, so a second pass sees what the first one
/// wrote. The profiles and expected gains are the Python vectors.
library;

import 'dart:convert';
import 'dart:io';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart' show EncryptedField;
import 'package:traxjourney_client/src/crypto/encryption_migration.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

const _trip = ProjectRef(name: 'Trip');

bool _isEnv(Object? v) => v is String && EncryptedField.isEnvelope(v);

/// One `elevation_gain` vector: its series and the gain Python measured.
({List<double> distances, List<double> elevations, double gain}) _vector(String name) {
  final all = jsonDecode(File('test/fixtures/track_metrics_vectors.json').readAsStringSync())
      as Map<String, dynamic>;
  final c = (all['elevation_gain'] as List)
      .cast<Map<String, dynamic>>()
      .singleWhere((c) => c['name'] == name);
  final input = c['input'] as Map<String, dynamic>;
  List<double> doubles(Object? v) => [for (final x in v as List) (x as num).toDouble()];
  return (
    distances: doubles(input['distances_km']),
    elevations: doubles(input['elevations']),
    gain: (c['expected'] as num).toDouble(),
  );
}

String _profileJson(List<double> distances, List<double> elevations) =>
    jsonEncode({'distances_km': distances, 'elevations_m': elevations});

/// One trip as the database holds it, served as the API does.
class _Server {
  int lockVersion = 10;
  final Map<int, Map<String, Object?>> activities = {};
  final Map<int, Map<String, Object?>> memories = {};

  /// The next write is refused as stale: another device wrote just before.
  bool staleNext = false;

  final List<http.Request> log = [];

  List<http.Request> get writes => log.where((r) => r.method == 'PUT').toList();
  List<http.Request> get gainWrites =>
      writes.where((r) => r.url.path.endsWith('/elevation-gain')).toList();
  List<http.Request> get profileWrites =>
      writes.where((r) => RegExp(r'^/api/activities/-?\d+$').hasMatch(r.url.path)).toList();
  List<String> get trackGets => [
        for (final r in log)
          if (r.method == 'GET' && r.url.path.endsWith('/track')) r.url.path
      ];

  static const _columns = [
    'name', 'summary_polyline', 'start_latlng_json', 'end_latlng_json',
    'elevation_profile_json', 'elevation_profile_low_res_json',
  ];

  List<String> plainFields(Map<String, Object?> row) => [
        for (final c in _columns)
          if (row[c] is String && (row[c] as String).isNotEmpty && !_isEnv(row[c])) c
      ];

  List<List<num>>? _pairs(Object? json) {
    if (json is! String || _isEnv(json)) return null;
    final ep = jsonDecode(json) as Map<String, dynamic>;
    final d = (ep['distances_km'] as List).cast<num>();
    final e = (ep['elevations_m'] as List).cast<num>();
    return [for (var i = 0; i < d.length; i++) [d[i], e[i]]];
  }

  /// `_row_to_activity` + `to_strava_dict`; [heavy] loads the deferred
  /// full profile, else `elevation_profile_enc` falls back to the low-res one.
  Map<String, dynamic> activityJson(int id, {required bool heavy}) {
    final row = activities[id]!;
    final full = heavy ? row['elevation_profile_json'] : null;
    final low = row['elevation_profile_low_res_json'];
    Object? enc(Object? v) => _isEnv(v) ? v : null;
    return {
      'id': id,
      'name': row['name'],
      'is_edited': row['is_edited'],
      'source': row['source'],
      'total_elevation_gain': row['total_elevation_gain'],
      'elevation_profile': _pairs(full) ?? _pairs(low),
      'elevation_profile_enc': enc(full) ?? enc(low),
    };
  }

  /// The `/meta` payload. `ProjectIO.to_dict` adds `plain_fields` and
  /// `has_gain_snapshot` (U15) to each activity; a row without the latter
  /// key stands for a server that does not send it yet.
  Map<String, dynamic> meta() => {
        'name': 'Trip',
        'lock_version': lockVersion,
        'caller_role': 'owner',
        'activities': [
          for (final id in activities.keys)
            {
              ...activityJson(id, heavy: false),
              'plain_fields': plainFields(activities[id]!),
              if (activities[id]!.containsKey('has_gain_snapshot'))
                'has_gain_snapshot': activities[id]!['has_gain_snapshot'],
            },
        ],
        'items': [
          for (final e in memories.entries)
            {'item_type': 'memory', 'memory': {'id': e.key, ...e.value}},
        ],
      };

  http.Response _json(Object body, [int status = 200]) =>
      http.Response(jsonEncode(body), status);

  http.Response? _cas(Object? expected) {
    if (staleNext) {
      staleNext = false;
      lockVersion++;
      return _json({
        'detail': {'code': 'stale_write', 'message': 'The trip changed', 'project_id': 1}
      }, 409);
    }
    expect(expected, lockVersion, reason: 'every catch-up write is a CAS');
    lockVersion++;
    return null;
  }

  Future<http.Response> handle(http.Request req) async {
    final path = req.url.path;
    log.add(req);
    if (req.method == 'GET') {
      final t = RegExp(r'^/api/projects/Trip/activities/(-?\d+)/track$').firstMatch(path);
      if (t != null) {
        return _json({
          ...activityJson(int.parse(t.group(1)!), heavy: true),
          'lock_version': lockVersion,
        });
      }
      return http.Response('not found', 404);
    }
    final body = jsonDecode(req.body) as Map<String, dynamic>;
    final gain = RegExp(r'^/api/activities/(-?\d+)/elevation-gain$').firstMatch(path);
    if (gain != null) {
      final row = activities[int.parse(gain.group(1)!)]!;
      if (!_isEnv(row['elevation_profile_json'])) {
        return _json({'detail': {'code': 'not_encrypted'}}, 409);
      }
      final refusal = _cas(body['lock_version']);
      if (refusal != null) return refusal;
      expect(body['project'], 'Trip');
      row['total_elevation_gain'] = body['total_elevation_gain'];
      return _json({'id': 0, 'lock_version': lockVersion});
    }
    final a = RegExp(r'^/api/activities/(-?\d+)$').firstMatch(path);
    if (a != null) {
      final refusal = _cas(body.remove('lock_version'));
      if (refusal != null) return refusal;
      expect(body.remove('project'), 'Trip');
      activities[int.parse(a.group(1)!)]!.addAll(body);
      return _json({'id': 0, 'lock_version': lockVersion});
    }
    final m = RegExp(r'^/api/memories/(\d+)$').firstMatch(path);
    if (m != null) {
      final refusal = _cas(body.remove('lock_version'));
      if (refusal != null) return refusal;
      memories[int.parse(m.group(1)!)]!.addAll(body);
      return _json({'lock_version': lockVersion});
    }
    return http.Response('not found', 404);
  }
}

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

class _FakeApi implements EncryptionApi {
  @override
  Future<void> enable(Map<String, dynamic> payload) async {}
  @override
  Future<EncryptionStatus> fetchStatus(String? d) async => const EncryptionStatus(
      enabled: false, recoveryMethods: [], deviceRegistered: false, deviceApproved: false);
  @override
  Future<void> registerDevice(String a, String b) async {}
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String a, String b, String c) async {}
  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String m) async => null;
  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {}
  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async =>
      wrappedCmkB64;
}

/// Counts the envelopes it decrypts: a converged row must not be read again.
class _CountingService extends EncryptionService {
  _CountingService() : super(_FakeStore(), _FakeApi());

  int decrypts = 0;

  @override
  Future<String> decryptText(String envelope) {
    decrypts++;
    return super.decryptText(envelope);
  }
}

/// A session token for [userId], shaped like the server's JWT.
String _token(int userId) {
  String part(Object json) => base64Url.encode(utf8.encode(jsonEncode(json)));
  return '${part({'alg': 'HS256'})}.${part({'sub': '$userId'})}.sig';
}

late _Server _server;
late ApiClient _api;
late _CountingService _enc;
late EncryptionMigration _migration;

/// One catch-up pass over the trip as the server holds it now.
Future<CatchUpResult> _pass() {
  final trip = CatchUpPayload.of(_server.meta());
  return _migration.encryptTrip(_trip, trip,
      role: 'owner', lockVersion: trip.lockVersion!);
}

/// An activity row whose geometry the enable-time migration already
/// encrypted, the low-res column holding the full profile's envelope (R4-5).
Future<Map<String, Object?>> _encryptedActivity({
  required String profileJson,
  required double gain,
  bool isEdited = false,
  String source = 'strava',
  bool? hasGainSnapshot,
  EncryptionService? under,
}) async {
  final key = under ?? _enc;
  final profile = await key.encryptText(profileJson);
  return {
    'name': await key.encryptText('Ride'),
    'summary_polyline': await key.encryptText('_p~iF~ps|U'),
    'elevation_profile_json': profile,
    'elevation_profile_low_res_json': profile,
    'is_edited': isEdited,
    'source': source,
    'total_elevation_gain': gain,
    if (hasGainSnapshot != null) 'has_gain_snapshot': hasGainSnapshot,
  };
}

Future<Map<String, dynamic>> _decryptedProfile(Object? env) async =>
    jsonDecode(await _enc.decryptText(env! as String)) as Map<String, dynamic>;

void main() {
  setUp(() async {
    _server = _Server();
    _api = ApiClient(baseUrl: '', httpClient: MockClient((r) => _server.handle(r)))
      ..setToken(_token(1));
    _enc = _CountingService();
    await _enc.enable(const RecoveryKeyChoice());
    _migration = EncryptionMigration(_api, _enc);
    EncryptionMigration.resetConvergedForTest();
  });

  test('an inflated edited activity gets exactly one gain write, with the vector value',
      () async {
    final v = _vector('climb_barometric');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations), gain: 900.0, isEdited: true);

    final result = await _pass();

    expect(_server.gainWrites, hasLength(1));
    expect(_server.profileWrites, isEmpty);
    final body = jsonDecode(_server.gainWrites.single.body) as Map<String, dynamic>;
    expect(_server.gainWrites.single.url.path, '/api/activities/-5/elevation-gain');
    expect(body['total_elevation_gain'] as double, closeTo(v.gain, 1e-6));
    expect(body['project'], 'Trip');
    expect(body['lock_version'], 10);
    expect(result.written, 1);
    expect(result.complete, isTrue);
    // Measured from the payload's envelope: nothing listed, so no /track.
    expect(_server.trackGets, isEmpty);

    _server.log.clear();
    await _pass();
    expect(_server.writes, isEmpty);
  });

  test('an edit with no gain snapshot (before #386) is recomputed', () async {
    final v = _vector('climb_barometric');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations),
        gain: 900.0,
        isEdited: true,
        hasGainSnapshot: false);

    await _pass();

    expect(_server.gainWrites, hasLength(1));
    expect(_server.activities[-5]!['total_elevation_gain'] as double, closeTo(v.gain, 1e-6));
  });

  test('an edit with a gain snapshot (since #386) is never touched, not even repaired',
      () async {
    // Its profile came from the server's own fixed pipeline or the device
    // port, and its gain is the Strava figure scaled by the edit.
    final v = _vector('sentinel_dropouts_as_stored_before_374');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations),
        gain: 5000.0,
        isEdited: true,
        hasGainSnapshot: true);

    await _pass();

    expect(_server.log, isEmpty);
  });

  test('a GPX activity is recomputed even when it has a gain snapshot', () async {
    final v = _vector('climb_barometric');
    _server.activities[-6] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations),
        gain: 900.0,
        isEdited: true,
        source: 'gpx',
        hasGainSnapshot: true);

    await _pass();

    expect(_server.gainWrites, hasLength(1));
    expect(_server.activities[-6]!['total_elevation_gain'] as double, closeTo(v.gain, 1e-6));
  });

  test('a GPX activity is selected too, edited or not', () async {
    final v = _vector('rollers_barometric');
    _server.activities[-6] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations), gain: 400.0, source: 'gpx');

    await _pass();

    expect(_server.gainWrites, hasLength(1));
    expect(_server.activities[-6]!['total_elevation_gain'] as double, closeTo(v.gain, 1e-6));
  });

  test('a gain within 0.5 m of the measured one triggers no write', () async {
    final v = _vector('climb_barometric');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations),
        gain: v.gain + 0.4,
        isEdited: true);

    await _pass();

    expect(_server.writes, isEmpty);
  });

  test('an unedited Strava activity is never touched', () async {
    final v = _vector('sentinel_dropouts_as_stored_before_374');
    _server.activities[42] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations), gain: 5000.0);

    await _pass();

    expect(_server.log, isEmpty);
  });

  test('a sentinel profile is repaired, written once to both columns, then its gain',
      () async {
    final stored = _vector('sentinel_dropouts_as_stored_before_374');
    final repaired = _vector('sentinel_dropouts_after_repair');
    _server.activities[-7] = await _encryptedActivity(
        profileJson: _profileJson(stored.distances, stored.elevations),
        gain: stored.gain,
        source: 'gpx');

    final result = await _pass();

    // The profile first, then the gain measured from the repaired series.
    expect([for (final w in _server.writes) w.url.path],
        ['/api/activities/-7', '/api/activities/-7/elevation-gain']);
    expect([for (final w in _server.writes) jsonDecode(w.body)['lock_version']], [10, 11]);
    final profileBody = jsonDecode(_server.profileWrites.single.body) as Map<String, dynamic>;
    expect(profileBody.keys.toSet(), {
      'elevation_profile_json', 'elevation_profile_low_res_json', 'project', 'lock_version',
    });

    final row = _server.activities[-7]!;
    expect(_isEnv(row['elevation_profile_json']), isTrue);
    expect(row['elevation_profile_low_res_json'], row['elevation_profile_json']);
    final ep = await _decryptedProfile(row['elevation_profile_json']);
    expect(ep['distances_km'], stored.distances);
    final elevations = (ep['elevations_m'] as List).cast<num>();
    expect(elevations, hasLength(repaired.elevations.length));
    for (var i = 0; i < elevations.length; i++) {
      expect(elevations[i].toDouble(), closeTo(repaired.elevations[i], 1e-6), reason: '[$i]');
    }
    expect(row['total_elevation_gain'] as double, closeTo(repaired.gain, 1e-6));
    expect(result.written, 2);

    _server.log.clear();
    await _pass();
    expect(_server.writes, isEmpty);
  });

  test('a repaired profile whose gain was already right writes the profile only',
      () async {
    final stored = _vector('sentinel_dropouts_as_stored_before_374');
    final repaired = _vector('sentinel_dropouts_after_repair');
    _server.activities[-7] = await _encryptedActivity(
        profileJson: _profileJson(stored.distances, stored.elevations),
        gain: repaired.gain,
        isEdited: true);

    await _pass();

    expect([for (final w in _server.writes) w.url.path], ['/api/activities/-7']);
  });

  test('a profile fetched by the pass from GET …/track is the one measured', () async {
    // The full profile is an envelope but the low-res copy is still
    // plaintext, so /meta carries no envelope: the pass reads /track for the
    // low-res column and measures the full profile from that answer.
    final v = _vector('climb_barometric');
    final row = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations), gain: 900.0, isEdited: true);
    row['elevation_profile_low_res_json'] = _profileJson([0.0, 1.0], [300.0, 500.0]);
    _server.activities[-5] = row;

    await _pass();

    expect(_server.trackGets, ['/api/projects/Trip/activities/-5/track']);
    expect([for (final w in _server.writes) w.url.path],
        ['/api/activities/-5', '/api/activities/-5/elevation-gain']);
    expect([for (final w in _server.writes) jsonDecode(w.body)['lock_version']], [10, 11]);
    expect(_server.activities[-5]!['total_elevation_gain'] as double, closeTo(v.gain, 1e-6));
  });

  test('a profile the pass encrypts itself is left to the next pass', () async {
    final v = _vector('climb_barometric');
    final profile = _profileJson(v.distances, v.elevations);
    _server.activities[-5] = {
      'name': 'Ride',
      'elevation_profile_json': profile,
      'elevation_profile_low_res_json': profile,
      'is_edited': true,
      'source': 'strava',
      'total_elevation_gain': 900.0,
    };

    await _pass();

    // Encrypted, but its stored figure came from the server's own pipeline
    // while the profile was readable: no gain write in this pass.
    expect(_server.gainWrites, isEmpty);
    expect(_isEnv(_server.activities[-5]!['elevation_profile_json']), isTrue);
  });

  test("another account's envelope is skipped without counting as a failure", () async {
    final other = EncryptionService(_FakeStore(), _FakeApi());
    await other.enable(const RecoveryKeyChoice());
    final v = _vector('climb_barometric');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations),
        gain: 900.0,
        isEdited: true,
        under: other);

    final result = await _pass();

    expect(_server.writes, isEmpty);
    expect(result.complete, isTrue);
  });

  test('a stale_write on the gain write ends the pass', () async {
    final v = _vector('climb_barometric');
    _server.activities[-5] = await _encryptedActivity(
        profileJson: _profileJson(v.distances, v.elevations), gain: 900.0, isEdited: true);
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    _server.staleNext = true;

    final result = await _pass();

    expect([for (final w in _server.writes) w.url.path],
        ['/api/activities/-5/elevation-gain']);
    expect(result.ended, 1);
    expect(_server.memories[1]!['name'], 'Summit');
    expect(_server.activities[-5]!['total_elevation_gain'], 900.0);
  });

  group('a converged row is not re-measured on the next load (U8-R1-1)', () {
    test('a correct gain: the second pass decrypts nothing and sends nothing',
        () async {
      final v = _vector('climb_barometric');
      _server.activities[-5] = await _encryptedActivity(
          profileJson: _profileJson(v.distances, v.elevations),
          gain: v.gain,
          isEdited: true);
      await _pass();
      expect(_enc.decrypts, 1);

      _enc.decrypts = 0;
      _server.log.clear();
      // A new migration object, as each load builds one.
      _migration = EncryptionMigration(_api, _enc);
      await _pass();

      expect(_enc.decrypts, 0);
      expect(_server.writes, isEmpty);
    });

    test('after its repair and gain writes, the row is not read again', () async {
      final stored = _vector('sentinel_dropouts_as_stored_before_374');
      _server.activities[-7] = await _encryptedActivity(
          profileJson: _profileJson(stored.distances, stored.elevations),
          gain: stored.gain,
          source: 'gpx');
      await _pass();
      expect(_server.writes, hasLength(2));

      _enc.decrypts = 0;
      _server.log.clear();
      await _pass();

      expect(_enc.decrypts, 0);
      expect(_server.writes, isEmpty);
    });

    test('a stored gain changed since (another device) is measured again', () async {
      final v = _vector('climb_barometric');
      _server.activities[-5] = await _encryptedActivity(
          profileJson: _profileJson(v.distances, v.elevations),
          gain: v.gain,
          isEdited: true);
      await _pass();
      expect(_server.writes, isEmpty);

      _server.activities[-5]!['total_elevation_gain'] = 900.0;
      _enc.decrypts = 0;
      await _pass();

      expect(_enc.decrypts, 1);
      expect(_server.gainWrites, hasLength(1));
      expect(_server.activities[-5]!['total_elevation_gain'] as double,
          closeTo(v.gain, 1e-6));
    });

    test('a changed profile envelope is measured again', () async {
      final v = _vector('climb_barometric');
      final profile = _profileJson(v.distances, v.elevations);
      _server.activities[-5] = await _encryptedActivity(
          profileJson: profile, gain: v.gain, isEdited: true);
      await _pass();

      // The same series re-encrypted (an edit, a repair): a new envelope.
      final env = await _enc.encryptText(profile);
      _server.activities[-5]!['elevation_profile_json'] = env;
      _server.activities[-5]!['elevation_profile_low_res_json'] = env;
      _enc.decrypts = 0;
      await _pass();

      expect(_enc.decrypts, 1);
      expect(_server.writes, isEmpty);
    });
  });
}
