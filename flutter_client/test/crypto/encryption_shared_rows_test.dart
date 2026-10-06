/// The catch-up pass and activity rows another user's trip also holds (E2EE
/// remnants decision 15): such a row stays readable to that trip, so the pass
/// never encrypts it, counts it as staying unencrypted, and writes back in
/// plaintext the envelopes the user's key can open (left by the shipped
/// migration or an earlier pass). The gain recompute of decision 12 leaves
/// it alone.
///
/// Every test runs [EncryptionMigration.encryptTrip] against [_Server], which
/// stores rows the way the database does, builds `/meta` (with
/// `shared_with_others`) and `GET …/track` from them, and refuses an envelope
/// on a shared row with 409 `shared_with_other_trip` as U16's server does.
library;

import 'dart:convert';

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

const _polyline = '_p~iF~ps|U_ulLnnqC_mqNvxq`@';
const _fullProfile = '{"distances_km":[0.0,1.0,2.0,3.0],"elevations_m":[10.0,20.0,30.0,40.0]}';
const _lowResProfile = '{"distances_km":[0.0,3.0],"elevations_m":[10.0,40.0]}';

const _columns = [
  'name', 'summary_polyline', 'start_latlng_json', 'end_latlng_json',
  'elevation_profile_json', 'elevation_profile_low_res_json',
  'original_polyline', 'original_elevation_profile_json',
  'original_start_latlng_json', 'original_end_latlng_json',
];

bool _isEnv(Object? v) => v is String && EncryptedField.isEnvelope(v);

/// A plaintext activity row as Strava/GPX import stores it.
Map<String, Object?> _plainActivity(String name) => {
      'name': name,
      'summary_polyline': _polyline,
      'start_latlng_json': '[45.1, 6.1]',
      'end_latlng_json': '[45.2, 6.2]',
      'elevation_profile_json': _fullProfile,
      'elevation_profile_low_res_json': _lowResProfile,
      'is_edited': false,
      'source': 'strava',
      'total_elevation_gain': 30.0,
    };

/// The plaintext of an edited row, its four snapshots included.
Map<String, Object?> _plainEdited(String name) => {
      ..._plainActivity(name),
      'is_edited': true,
      'original_polyline': '_ibE_seK_seK_seK',
      'original_elevation_profile_json': _fullProfile,
      'original_start_latlng_json': '[45.0, 6.0]',
      'original_end_latlng_json': '[45.3, 6.3]',
    };

/// [plain] as the shipped migration left it under [key]: every E2EE column
/// an envelope, the low-res one the full profile's (R4-5).
Future<Map<String, Object?>> _encrypted(
    Map<String, Object?> plain, EncryptionService key) async {
  final row = {...plain};
  for (final c in _columns) {
    final v = row[c];
    if (v is String) row[c] = await key.encryptText(v);
  }
  row['elevation_profile_low_res_json'] = row['elevation_profile_json'];
  return row;
}

/// One trip as the database holds it, served as the API does.
class _Server {
  int lockVersion = 10;
  final Map<int, Map<String, Object?>> activities = {};
  final Map<int, Map<String, Object?>> memories = {};

  /// Rows a trip owned by someone else also holds.
  final Set<int> shared = {};

  /// Rows that become shared after `/meta` was built: another traveller's
  /// trip takes them while the pass runs.
  final Set<int> sharedLater = {};

  /// Every request is refused with 401 from then on.
  bool unauthorized = false;

  /// The next write is refused as stale: another device wrote just before.
  bool staleNext = false;

  final List<http.Request> log = [];

  List<http.Request> get writes => log.where((r) => r.method == 'PUT').toList();
  List<String> get writePaths => [for (final w in writes) w.url.path];
  List<String> get trackGets => [
        for (final r in log)
          if (r.method == 'GET' && r.url.path.endsWith('/track')) r.url.path
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

  /// `_row_to_activity` + `to_strava_dict`; [heavy] loads the deferred columns.
  Map<String, dynamic> activityJson(int id, {required bool heavy}) {
    final row = activities[id]!;
    final full = heavy ? row['elevation_profile_json'] : null;
    final low = row['elevation_profile_low_res_json'];
    Object? parsed(Object? v) => v is String && !_isEnv(v) ? jsonDecode(v) : null;
    Object? enc(Object? v) => _isEnv(v) ? v : null;
    return {
      'id': id,
      'name': row['name'],
      'is_edited': row['is_edited'],
      'source': row['source'],
      'total_elevation_gain': row['total_elevation_gain'],
      'map': {'summary_polyline': heavy ? row['summary_polyline'] : null},
      'start_latlng': parsed(row['start_latlng_json']),
      'start_latlng_enc': enc(row['start_latlng_json']),
      'end_latlng': parsed(row['end_latlng_json']),
      'end_latlng_enc': enc(row['end_latlng_json']),
      'elevation_profile': _pairs(full) ?? _pairs(low),
      'elevation_profile_enc': enc(full) ?? enc(low),
    };
  }

  Map<String, dynamic> meta() => {
        'name': 'Trip',
        'lock_version': lockVersion,
        'caller_role': 'owner',
        'activities': [
          for (final id in activities.keys)
            {
              ...activityJson(id, heavy: false),
              'plain_fields': plainFields(activities[id]!),
              'has_gain_snapshot': false,
              'shared_with_others': shared.contains(id),
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

  http.Response _sharedRefusal(int id) => _json({
        'detail': {
          'code': 'shared_with_other_trip',
          'message': "This activity is also in another traveller's trip, so it "
              'stays unencrypted.',
          'activity_id': id,
        }
      }, 409);

  bool _isShared(int id) => shared.contains(id) || sharedLater.contains(id);

  Future<http.Response> handle(http.Request req) async {
    final path = req.url.path;
    log.add(req);
    if (unauthorized) return http.Response('{"detail":"Not authenticated"}', 401);
    if (req.method == 'GET') {
      final t = RegExp(r'^/api/projects/Trip/activities/(-?\d+)/track$').firstMatch(path);
      if (t != null) {
        final id = int.parse(t.group(1)!);
        final row = activities[id]!;
        return _json({
          ...activityJson(id, heavy: true),
          if (row['is_edited'] == true)
            for (final c in _columns.where((c) => c.startsWith('original_'))) c: row[c],
          'lock_version': lockVersion,
        });
      }
      return http.Response('not found', 404);
    }
    final body = jsonDecode(req.body) as Map<String, dynamic>;
    final gain = RegExp(r'^/api/activities/(-?\d+)/elevation-gain$').firstMatch(path);
    if (gain != null) {
      final id = int.parse(gain.group(1)!);
      if (_isShared(id)) return _sharedRefusal(id);
      final refusal = _cas(body['lock_version']);
      if (refusal != null) return refusal;
      activities[id]!['total_elevation_gain'] = body['total_elevation_gain'];
      return _json({'id': id, 'lock_version': lockVersion});
    }
    final a = RegExp(r'^/api/activities/(-?\d+)$').firstMatch(path);
    if (a != null) {
      final id = int.parse(a.group(1)!);
      final cas = body.remove('lock_version');
      expect(body.remove('project'), 'Trip');
      if (_isShared(id) && body.values.any(_isEnv)) return _sharedRefusal(id);
      final refusal = _cas(cas);
      if (refusal != null) return refusal;
      activities[id]!.addAll(body);
      return _json({'id': id, 'lock_version': lockVersion});
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
}

/// A session token for [userId], shaped like the server's JWT.
String _token(int userId) {
  String part(Object json) => base64Url.encode(utf8.encode(jsonEncode(json)));
  return '${part({'alg': 'HS256'})}.${part({'sub': '$userId'})}.sig';
}

late _Server _server;
late EncryptionService _enc;
late EncryptionService _other;
late EncryptionMigration _migration;

/// One catch-up pass over the trip as the server holds it now.
Future<CatchUpResult> _pass() {
  final trip = CatchUpPayload.of(_server.meta());
  return _migration.encryptTrip(_trip, trip,
      role: 'owner', lockVersion: trip.lockVersion!);
}

void main() {
  setUp(() async {
    _server = _Server();
    final api = ApiClient(baseUrl: '', httpClient: MockClient((r) => _server.handle(r)))
      ..setToken(_token(1));
    _enc = EncryptionService(_FakeStore(), _FakeApi());
    await _enc.enable(const RecoveryKeyChoice());
    _other = EncryptionService(_FakeStore(), _FakeApi());
    await _other.enable(const RecoveryKeyChoice());
    _migration = EncryptionMigration(api, _enc);
    EncryptionMigration.resetConvergedForTest();
  });

  test('a shared plaintext row is not encrypted, not read, and counted', () async {
    _server.activities[-5] = _plainActivity('Ride with Ana');
    _server.shared.add(-5);
    _server.activities[-6] = _plainActivity('My own ride');

    final result = await _pass();

    expect(_server.activities[-5], _plainActivity('Ride with Ana'));
    expect(_server.trackGets, ['/api/projects/Trip/activities/-6/track']);
    expect(_server.writePaths, ['/api/activities/-6']);
    expect(_server.plainFields(_server.activities[-6]!), isEmpty);
    expect(result.unencryptable, 1);
    expect(result.written, 1);
    expect(result.complete, isFalse);

    // Counted again on every pass: it stays unencrypted.
    _server.log.clear();
    final again = await _pass();
    expect(_server.log, isEmpty);
    expect(again.unencryptable, 1);
  });

  test('an unedited plaintext row is not read either', () async {
    // No snapshot columns to clear on an unedited row.
    _server.activities[-5] = _plainActivity('Ride with Ana');
    _server.shared.add(-5);

    await _pass();

    expect(_server.trackGets, isEmpty);
  });

  test('a shared row under my key is written back in plaintext, every field, '
      'in the CAS chain', () async {
    _server.activities[-4] = _plainActivity('My own ride');
    _server.activities[-5] = await _encrypted(_plainEdited('Ride with Ana'), _enc);
    _server.shared.add(-5);
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};

    final result = await _pass();

    final row = _server.activities[-5]!;
    final expected = _plainEdited('Ride with Ana');
    for (final c in _columns.where((c) => c != 'elevation_profile_low_res_json')) {
      expect(row[c], expected[c], reason: c);
    }
    // The low-res column gets the server's downsample of the full profile:
    // the 4-sample profile itself.
    expect(jsonDecode(row['elevation_profile_low_res_json']! as String),
        jsonDecode(_fullProfile));
    expect(_server.plainFields(row), _columns);

    // One write per row, each at the version the previous one returned.
    expect(_server.writePaths,
        ['/api/activities/-4', '/api/activities/-5', '/api/memories/1']);
    expect([for (final w in _server.writes) jsonDecode(w.body)['lock_version']],
        [10, 11, 12]);
    expect(result.written, 3);
    expect(result.unencryptable, 1);

    // Nothing left to repair: the next pass neither reads nor writes it.
    _server.log.clear();
    final again = await _pass();
    expect(_server.log, isEmpty);
    expect(again.unencryptable, 1);
  });

  test("another account's envelopes are left alone, counted, and not read again",
      () async {
    final foreign = await _encrypted(_plainActivity("Ana's ride"), _other);
    _server.activities[-5] = {...foreign};
    _server.shared.add(-5);

    final result = await _pass();

    expect(_server.writes, isEmpty);
    expect(_server.activities[-5], foreign);
    expect(result.unencryptable, 1);
    expect(result.skipped, 0);
    expect(_server.trackGets, hasLength(1));

    _server.log.clear();
    await _pass();
    expect(_server.trackGets, isEmpty, reason: 'found with nothing to decrypt');
  });

  test('a row holding envelopes under both keys gets only mine back', () async {
    final row = await _encrypted(_plainActivity('Ride with Ana'), _other);
    row['name'] = await _enc.encryptText('Ride with Ana');
    _server.activities[-5] = row;
    _server.shared.add(-5);

    await _pass();

    final body = jsonDecode(_server.writes.single.body) as Map<String, dynamic>;
    expect(body.keys.toSet(), {'name', 'project', 'lock_version'});
    expect(_server.activities[-5]!['name'], 'Ride with Ana');
    expect(_server.activities[-5]!['summary_polyline'], row['summary_polyline']);
  });

  test('a full profile in plaintext is kept; only the low-res envelope is '
      'decrypted, into its own column', () async {
    final row = _plainActivity('Ride with Ana');
    row['elevation_profile_low_res_json'] = await _enc.encryptText(_lowResProfile);
    _server.activities[-5] = row;
    _server.shared.add(-5);

    await _pass();

    final body = jsonDecode(_server.writes.single.body) as Map<String, dynamic>;
    expect(body.containsKey('elevation_profile_json'), isFalse);
    expect(jsonDecode(body['elevation_profile_low_res_json'] as String),
        jsonDecode(_lowResProfile));
  });

  test("the low-res column is the server's downsample of the decrypted profile",
      () async {
    // Integral values, so the selection reads off the distances. Expected
    // from src/project/elevation_downsample.downsample_elevation on the same
    // series: 300 points, index sum 149655, sum of squares 99890653.
    const n = 1000;
    final d = [for (var i = 0; i < n; i++) i.toDouble()];
    final e = [for (var i = 0; i < n; i++) ((i * 37) % 101 + (i ~/ 50) * 3).toDouble()];
    final profile = jsonEncode({'distances_km': d, 'elevations_m': e});
    final row = await _encrypted(_plainActivity('Long ride'), _enc);
    row['elevation_profile_json'] = await _enc.encryptText(profile);
    row['elevation_profile_low_res_json'] = row['elevation_profile_json'];
    _server.activities[-5] = row;
    _server.shared.add(-5);

    await _pass();

    final stored = _server.activities[-5]!;
    expect(stored['elevation_profile_json'], profile);
    final low = jsonDecode(stored['elevation_profile_low_res_json']! as String) as Map;
    final idx = [for (final v in low['distances_km'] as List) (v as num).toInt()];
    expect(idx, hasLength(300));
    expect(idx.fold<int>(0, (a, i) => a + i), 149655);
    expect(idx.fold<int>(0, (a, i) => a + i * i), 99890653);
    expect([for (final i in idx) e[i]], [
      for (final v in low['elevations_m'] as List) (v as num).toDouble()
    ]);
  });

  test('the gain recompute skips a shared row', () async {
    // A legacy GPX row whose stored gain is far off: unshared, the recompute
    // would correct it (encryption_gain_recompute_test.dart).
    final foreign = await _encrypted(
        {..._plainActivity('GPX with Ana'), 'source': 'gpx', 'total_elevation_gain': 900.0},
        _other);
    _server.activities[-5] = {...foreign};
    final mine = await _encrypted(
        {..._plainActivity('My GPX'), 'source': 'gpx', 'total_elevation_gain': 900.0}, _enc);
    _server.activities[-6] = mine;
    _server.shared.addAll([-5, -6]);

    final result = await _pass();

    expect(_server.writePaths.where((p) => p.endsWith('/elevation-gain')), isEmpty);
    // -6 was repaired to plaintext, its stored gain untouched; -5 not written.
    expect(_server.writePaths, ['/api/activities/-6']);
    expect(_server.activities[-6]!['total_elevation_gain'], 900.0);
    expect(_server.activities[-6]!['name'], 'My GPX');
    expect(result.unencryptable, 2);
  });

  test('a row another trip takes after the load: 409 counts it, the pass goes on',
      () async {
    _server.activities[-5] = _plainActivity('Ride');
    _server.sharedLater.add(-5);
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};

    final result = await _pass();

    expect(_server.plainFields(_server.activities[-5]!), isNotEmpty);
    expect(_isEnv(_server.memories[1]!['name']), isTrue);
    expect([for (final w in _server.writes) jsonDecode(w.body)['lock_version']],
        [10, 10], reason: 'the refused write advanced nothing');
    expect(result.unencryptable, 1);
    expect(result.skipped, 0);
    expect(result.ended, 0);
  });

  test("a 409 shared_with_other_trip on the gain write counts the row", () async {
    _server.activities[-5] = await _encrypted(
        {..._plainActivity('GPX'), 'source': 'gpx', 'total_elevation_gain': 900.0}, _enc);
    _server.sharedLater.add(-5);

    final result = await _pass();

    expect(_server.activities[-5]!['total_elevation_gain'], 900.0);
    expect(result.unencryptable, 1);
    expect(result.skipped, 0);
  });

  test('a stale_write on the repair ends the pass', () async {
    _server.activities[-5] = await _encrypted(_plainActivity('Ride with Ana'), _enc);
    _server.shared.add(-5);
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    _server.staleNext = true;

    final result = await _pass();

    expect(_server.writePaths, ['/api/activities/-5']);
    expect(_server.memories[1]!['name'], 'Summit');
    expect(result.ended, 1);
  });

  test('a 401 on the repair read ends the pass with the session', () async {
    _server.activities[-5] = await _encrypted(_plainActivity('Ride with Ana'), _enc);
    _server.shared.add(-5);
    _server.unauthorized = true;

    final result = await _pass();

    expect(result.sessionEnded, isTrue);
    expect(_server.writes, isEmpty);
  });

  test('a /track at another lock version ends the pass before the repair',
      () async {
    _server.activities[-5] = await _encrypted(_plainActivity('Ride with Ana'), _enc);
    _server.shared.add(-5);
    final trip = CatchUpPayload.of(_server.meta());
    _server.lockVersion++; // another device wrote

    final result =
        await _migration.encryptTrip(_trip, trip, role: 'owner', lockVersion: 10);

    expect(_server.writes, isEmpty);
    expect(result.ended, 1);
  });

  test('CatchUpPayload keeps shared rows out of the encrypt and gain lists', () {
    final payload = CatchUpPayload.of({
      'lock_version': 1,
      'activities': [
        {
          'id': -5,
          'plain_fields': ['name'],
          'shared_with_others': true,
          'source': 'gpx',
          'name': 'Ride',
        },
        // Absent: not shared (an older server).
        {'id': -6, 'plain_fields': ['name'], 'name': 'Ride'},
      ],
    });
    expect([for (final a in payload.plainActivities) a.id], [-6]);
    expect([for (final a in payload.gainActivities) a.id], isEmpty);
    expect([for (final a in payload.sharedActivities) a.id], [-5]);
    expect(payload.sharedActivities.single.mayHoldEnvelope, isTrue,
        reason: 'no polyline listed: null or an envelope');
  });
}
