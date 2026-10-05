/// The catch-up encryption pass that runs after a trip loads (E2EE remnants
/// decision 6, decision 2 for the companion repair, decision 13 for its
/// compare-and-swap writes).
///
/// Every test drives the real post-load path of [ProjectNotifier] — `load()`
/// with the `/meta` payload shape, or the details-only reload — against
/// [_Server], a fake that stores rows the way the database does and builds
/// `/meta`, `GET …/track` and the write answers from them, so a second load
/// sees what the first one wrote.
///
/// The app's real `encryption` singleton is used; it talks to the server
/// through the `api` client current when it is first used, so all tests share
/// one client whose answers the current [_Server] decides.
library;

import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart' show EncryptedField;
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_migration.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ownTrip = ProjectRef(name: 'Trip');
const _companionTrip = ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');

const _polyline = '_p~iF~ps|U_ulLnnqC_mqNvxq`@';
const _fullProfile = '{"distances_km":[0.0,1.0,2.0,3.0],"elevations_m":[10.0,20.0,30.0,40.0]}';
const _lowResProfile = '{"distances_km":[0.0,3.0],"elevations_m":[10.0,40.0]}';

/// The activity columns the client encrypts, as the database names them.
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
    };

/// One trip as the database holds it, served as the API does.
class _Server {
  int lockVersion = 10;
  String callerRole = 'owner';
  final Map<int, Map<String, Object?>> activities = {};
  final Map<int, Map<String, Object?>> memories = {};
  final Map<int, Map<String, Object?>> journals = {};

  /// Activities whose E2EE fields this caller may not write (decision 14).
  final Set<int> refused = {};

  /// Runs once, when the first `GET …/track` arrives (another device writing).
  void Function()? onFirstTrackGet;

  /// The next write is refused as stale: another device wrote just before.
  bool staleNext = false;

  final List<http.Request> log = [];

  List<http.Request> get writes => log.where((r) => r.method == 'PUT').toList();
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

  /// `_row_to_activity` + `to_strava_dict`: parsed when plaintext, the
  /// envelope under `*_enc` otherwise; [heavy] loads the deferred columns.
  Map<String, dynamic> activityJson(int id, {required bool heavy}) {
    final row = activities[id]!;
    final full = heavy ? row['elevation_profile_json'] : null;
    final low = row['elevation_profile_low_res_json'];
    Object? parsed(Object? v) => v is String && !_isEnv(v) ? jsonDecode(v) : null;
    Object? enc(Object? v) => _isEnv(v) ? v : null;
    return {
      'id': id,
      'name': row['name'],
      'type': 'Ride',
      'start_date_local': '2026-01-01T09:00:00',
      'distance': 3000.0,
      'is_edited': row['is_edited'],
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
        'caller_role': callerRole,
        'activities': [
          for (final id in activities.keys)
            {...activityJson(id, heavy: false), 'plain_fields': plainFields(activities[id]!)},
        ],
        'items': [
          for (final id in activities.keys) {'item_type': 'activity', 'activity_id': id},
          for (final e in memories.entries)
            {'item_type': 'memory', 'memory': {'id': e.key, ...e.value}},
          for (final e in journals.entries)
            {'item_type': 'journal', 'journal': {'id': e.key, ...e.value}},
        ],
      };

  Map<String, dynamic> track(int id) {
    final row = activities[id]!;
    return {
      ...activityJson(id, heavy: true),
      if (row['is_edited'] == true)
        for (final c in _columns.where((c) => c.startsWith('original_'))) c: row[c],
      'lock_version': lockVersion,
    };
  }

  http.Response _json(Object body, [int status = 200]) =>
      http.Response(jsonEncode(body), status);

  http.Response _stale() => _json({
        'detail': {'code': 'stale_write', 'message': 'The trip changed', 'project_id': 1}
      }, 409);

  /// The CAS of decision 13: null when the write may proceed.
  http.Response? _cas(Object? expected) {
    if (staleNext) {
      staleNext = false;
      lockVersion++;
      return _stale();
    }
    if (expected != null && expected != lockVersion) return _stale();
    lockVersion++;
    return null;
  }

  Future<http.Response> handle(http.Request req) async {
    final path = req.url.path;
    if (path.startsWith('/api/encryption/')) return _json({});
    log.add(req);
    if (req.method == 'GET') {
      if (path == '/api/projects/Trip/meta') return _json(meta());
      if (path == '/api/projects/Trip') {
        final m = meta();
        m['activities'] = [
          for (final id in activities.keys)
            {...activityJson(id, heavy: true), 'plain_fields': plainFields(activities[id]!)},
        ];
        return _json(m);
      }
      final t = RegExp(r'^/api/projects/Trip/activities/(-?\d+)/track$').firstMatch(path);
      if (t != null) {
        final hook = onFirstTrackGet;
        onFirstTrackGet = null;
        hook?.call();
        return _json(track(int.parse(t.group(1)!)));
      }
      return http.Response('not found', 404);
    }
    if (req.method == 'PUT') {
      final body = jsonDecode(req.body) as Map<String, dynamic>;
      final a = RegExp(r'^/api/activities/(-?\d+)$').firstMatch(path);
      if (a != null) {
        final id = int.parse(a.group(1)!);
        if (refused.contains(id)) return http.Response('{"detail":"Activity not found"}', 404);
        final refusal = _cas(body.remove('lock_version'));
        if (refusal != null) return refusal;
        expect(body.remove('project'), 'Trip');
        activities[id]!.addAll(body);
        return _json({'id': id, 'lock_version': lockVersion});
      }
      final m = RegExp(r'^/api/(memories|journal)/(\d+)$').firstMatch(path);
      if (m != null) {
        final rows = m.group(1) == 'memories' ? memories : journals;
        final refusal = _cas(body.remove('lock_version'));
        if (refusal != null) return refusal;
        rows[int.parse(m.group(2)!)]!.addAll(body);
        return _json({'lock_version': lockVersion});
      }
    }
    return http.Response('not found', 404);
  }
}

late _Server _server;

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

/// An envelope under another account's key.
Future<String> _foreignEnvelope(String text) async {
  final other = EncryptionService(_FakeStore(), _FakeApi());
  await other.enable(const RecoveryKeyChoice());
  return other.encryptText(text);
}

Future<String> _decrypted(Object? v) => encryption.decryptText(v! as String);

/// Opens [ref] the way the trip screen does and waits for the pass.
Future<ProjectNotifier> _load(ProjectRef ref) async {
  final notifier = ProjectNotifier(ProjectService());
  await notifier.load(ref);
  await notifier.catchUpSettled();
  // And any pass the first one's refresh might have started.
  await pumpEventQueue();
  await notifier.catchUpSettled();
  return notifier;
}

void main() {
  setUpAll(() {
    // Before anything touches `encryption`: it keeps the client it first sees.
    api = ApiClient(baseUrl: '', httpClient: MockClient((r) => _server.handle(r)));
  });

  setUp(() async {
    projectDataCache.resetForTest();
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    _server = _Server();
    await encryption.enable(const RecoveryKeyChoice());
  });

  // A closure, not a tear-off: `encryption.lock` would create the singleton
  // here, before setUpAll has installed the fake client.
  tearDown(() => encryption.lock());

  test('one load of /meta encrypts a plaintext activity, memory and journal entry',
      () async {
    _server.activities[-5] = _plainActivity('Col du Galibier');
    _server.memories[1] = {'name': 'Summit', 'description': 'Cold wind', 'date': '2026-01-01'};
    _server.journals[2] = {'description': 'Long day', 'date': '2026-01-01'};

    await _load(_ownTrip);

    final act = _server.activities[-5]!;
    expect(_server.plainFields(act), isEmpty);
    expect(await _decrypted(act['name']), 'Col du Galibier');
    expect(await _decrypted(act['summary_polyline']), _polyline);
    expect(jsonDecode(await _decrypted(act['start_latlng_json'])), [45.1, 6.1]);
    expect(jsonDecode(await _decrypted(act['end_latlng_json'])), [45.2, 6.2]);
    expect(await _decrypted(_server.memories[1]!['name']), 'Summit');
    expect(await _decrypted(_server.memories[1]!['description']), 'Cold wind');
    expect(await _decrypted(_server.journals[2]!['description']), 'Long day');

    // Every write carried the pass's chained lock version, and the activity
    // write named its trip; the activity was read from /track, not /meta.
    final versions = [for (final w in _server.writes) jsonDecode(w.body)['lock_version']];
    expect(versions, [10, 11, 12]);
    expect(jsonDecode(_server.writes.first.body)['project'], 'Trip');
    expect(_server.trackGets, ['/api/projects/Trip/activities/-5/track']);
  });

  test('a second load of the now-encrypted trip sends no writes', () async {
    _server.activities[-5] = _plainActivity('Col du Galibier');
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    _server.journals[2] = {'description': 'Long day', 'date': '2026-01-01'};
    await _load(_ownTrip);
    expect(_server.writes, hasLength(3));

    _server.log.clear();
    await _load(_ownTrip);
    expect(_server.writes, isEmpty);
    expect(_server.trackGets, isEmpty);
  });

  test('the encrypted profile is the full one from GET …/track, in both columns',
      () async {
    _server.activities[-5] = _plainActivity('Ride');

    await _load(_ownTrip);

    final act = _server.activities[-5]!;
    expect(await _decrypted(act['elevation_profile_json']), _fullProfile);
    // The low-res column gets the full profile's envelope (R4-5): never the
    // /meta downsample, which is what the payload carried.
    expect(act['elevation_profile_low_res_json'], act['elevation_profile_json']);
  });

  test('an edited activity gets its originals written back encrypted, not null',
      () async {
    _server.activities[-5] = {
      ..._plainActivity('Trimmed'),
      'is_edited': true,
      'original_polyline': '_ibE_seK_seK_seK',
      'original_elevation_profile_json': _fullProfile,
      'original_start_latlng_json': '[45.0, 6.0]',
      'original_end_latlng_json': '[45.3, 6.3]',
    };

    await _load(_ownTrip);

    final act = _server.activities[-5]!;
    expect(_server.plainFields(act), isEmpty);
    expect(await _decrypted(act['original_polyline']), '_ibE_seK_seK_seK');
    expect(await _decrypted(act['original_elevation_profile_json']), _fullProfile);
    expect(await _decrypted(act['original_start_latlng_json']), '[45.0, 6.0]');
    expect(await _decrypted(act['original_end_latlng_json']), '[45.3, 6.3]');
  });

  test('a memory saved elsewhere after the load is not overwritten', () async {
    _server.activities[-5] = _plainActivity('Ride');
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    // Another device saves the memory between this load and the pass's first
    // GET …/track, which therefore answers at a newer lock version.
    _server.onFirstTrackGet = () {
      _server.memories[1]!['name'] = 'Summit, edited elsewhere';
      _server.lockVersion++;
    };

    await _load(_ownTrip);

    expect(_server.writes, isEmpty);
    expect(_server.memories[1]!['name'], 'Summit, edited elsewhere');
  });

  test('a stale_write on the first write stops the pass; the next load completes it',
      () async {
    _server.activities[-5] = _plainActivity('Ride');
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    _server.journals[2] = {'description': 'Long day', 'date': '2026-01-01'};
    _server.staleNext = true;

    await _load(_ownTrip);
    expect(_server.writes, hasLength(1));
    expect(_server.memories[1]!['name'], 'Summit');
    expect(_server.journals[2]!['description'], 'Long day');

    _server.log.clear();
    await _load(_ownTrip);
    expect(_server.writes, hasLength(3));
    expect(_server.plainFields(_server.activities[-5]!), isEmpty);
    expect(await _decrypted(_server.memories[1]!['name']), 'Summit');
    expect(await _decrypted(_server.journals[2]!['description']), 'Long day');
  });

  test('a 404 on one activity does not stop the others and sets the count to 1',
      () async {
    _server.activities[42] = _plainActivity("A companion's ride");
    _server.activities[-5] = _plainActivity('My ride');
    _server.refused.add(42);

    final notifier = await _load(_ownTrip);

    expect(_server.plainFields(_server.activities[42]!), isNotEmpty);
    expect(_server.plainFields(_server.activities[-5]!), isEmpty);
    expect(notifier.unencryptableActivityCount.value, 1);
    // The pass's own refresh after its write starts no second pass: the
    // refused row was read once.
    expect(_server.trackGets.where((p) => p.contains('/42/')), hasLength(1));
  });

  test("on a companion's trip: own journal encrypted, own-key memory restored, "
      'others untouched', () async {
    _server.callerRole = 'editor';
    _server.activities[-5] = _plainActivity("The owner's ride");
    final mine = await encryption.encryptText('My memory');
    final foreign = await _foreignEnvelope("Someone else's memory");
    _server.memories[1] = {'name': mine, 'date': '2026-01-01'};
    _server.memories[2] = {'name': foreign, 'date': '2026-01-01'};
    _server.memories[3] = {'name': 'Plain memory', 'date': '2026-01-01'};
    _server.journals[4] = {'description': 'My journal', 'date': '2026-01-01'};

    await _load(_companionTrip);

    expect(_server.memories[1]!['name'], 'My memory');
    expect(_server.memories[2]!['name'], foreign);
    expect(_server.memories[3]!['name'], 'Plain memory');
    expect(await _decrypted(_server.journals[4]!['description']), 'My journal');
    // The owner's activity is not the companion's to encrypt.
    expect(_server.trackGets, isEmpty);
    expect(_server.plainFields(_server.activities[-5]!), isNotEmpty);
    expect([for (final w in _server.writes) w.url.path],
        ['/api/memories/1', '/api/journal/4']);
  });

  test('a locked device runs no pass', () async {
    _server.activities[-5] = _plainActivity('Ride');
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    encryption.lock();

    await _load(_ownTrip);

    expect(_server.writes, isEmpty);
    expect(_server.trackGets, isEmpty);
  });

  test('the details-only reload runs the pass too', () async {
    _server.memories[1] = {'name': 'Summit', 'date': '2026-01-01'};
    final notifier = ProjectNotifier(ProjectService())..ref = _ownTrip;

    await notifier.reloadDetailsOnly(_ownTrip);
    await notifier.catchUpSettled();

    expect(await _decrypted(_server.memories[1]!['name']), 'Summit');
  });

  test('CatchUpPayload copies memory text before the reveal changes it', () {
    final memory = <String, dynamic>{'id': 1, 'name': 'v1.AA.BB'};
    final payload = CatchUpPayload.of({
      'lock_version': 3,
      'items': [
        {'item_type': 'memory', 'memory': memory},
      ],
    });
    memory['name'] = 'revealed';
    expect(payload.memories.single['name'], 'v1.AA.BB');
    expect(payload.lockVersion, 3);
  });
}
