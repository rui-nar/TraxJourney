import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_migration.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

/// Extends encryption_migration_test.dart's coverage to activity geometry
/// (issue #29, E2EE remnants decision 6): EncryptionMigration.run() encrypts
/// the fields an activity lists in `plain_fields`, read from
/// `GET …/track` — never from the trip payload, whose `/meta` form has no
/// polyline and only the downsampled profile — via a compare-and-swap
/// PUT /api/activities/{id}; it skips an activity with nothing listed and
/// leaves non-in-scope fields (e.g. kudos_count) untouched.
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
      enabled: false, recoveryMethods: [],
      deviceRegistered: false, deviceApproved: false);
  @override
  Future<void> registerDevice(String a, String b) async {}
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String a, String b, String c) async {}
  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String m) async => null;
}

const _allColumns = [
  'name', 'summary_polyline', 'start_latlng_json', 'end_latlng_json',
  'elevation_profile_json', 'elevation_profile_low_res_json',
];

/// A `/meta` activity: no polyline, the downsampled profile.
Map<String, dynamic> _metaActivity(int id, List<String> plainFields) => {
      'id': id,
      'name': 'Morning Ride',
      'kudos_count': 4,
      'map': {'summary_polyline': null},
      'start_latlng': [48.0, 2.0],
      'end_latlng': [48.5, 2.5],
      'elevation_profile': [
        [0.0, 10.0],
        [2.0, 30.0],
      ],
      'plain_fields': plainFields,
    };

/// `GET …/track` for the same activity: the full track and profile.
Map<String, dynamic> _track(int id, int lockVersion) => {
      'id': id,
      'name': 'Morning Ride',
      'kudos_count': 4,
      'map': {'summary_polyline': 'abc123xyz'},
      'start_latlng': [48.0, 2.0],
      'end_latlng': [48.5, 2.5],
      'elevation_profile': [
        [0.0, 10.0],
        [1.0, 20.0],
        [2.0, 30.0],
      ],
      'lock_version': lockVersion,
    };

/// Serves one trip of [activities] at lock version 1; records every request.
MockClient _server(List<Map<String, dynamic>> activities,
    Map<String, Map<String, dynamic>> puts, List<String> gets) {
  var lockVersion = 1;
  return MockClient((req) async {
    final path = req.url.path;
    if (req.method == 'GET' && path == '/api/projects/') {
      return http.Response(jsonEncode([{'name': 'Trip1'}]), 200);
    }
    if (req.method == 'GET') {
      gets.add(path);
      if (path == '/api/projects/Trip1/meta') {
        return http.Response(jsonEncode({
          'name': 'Trip1',
          'lock_version': lockVersion,
          'items': const [],
          'activities': activities,
        }), 200);
      }
      final m = RegExp(r'/activities/(\d+)/track$').firstMatch(path);
      if (m != null) {
        return http.Response(
            jsonEncode(_track(int.parse(m.group(1)!), lockVersion)), 200);
      }
    }
    if (req.method == 'PUT') {
      puts[path] = jsonDecode(req.body) as Map<String, dynamic>;
      lockVersion++;
      return http.Response(jsonEncode({'id': 0, 'lock_version': lockVersion}), 200);
    }
    return http.Response('not found', 404);
  });
}

void main() {
  test('encrypts a plaintext activity\'s listed fields from GET …/track',
      () async {
    final puts = <String, Map<String, dynamic>>{};
    final gets = <String>[];
    final mock = _server([_metaActivity(111, _allColumns)], puts, gets);

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    final result = await EncryptionMigration(api, enc).run();

    expect(result.written, 1);
    expect(gets, contains('/api/projects/Trip1/activities/111/track'));
    final body = puts['/api/activities/111']!;

    // In-scope fields are now ciphertext envelopes that decrypt back to the
    // exact plaintext (name/summary_polyline directly; the geometry fields via
    // the same JSON shape the DB column stores, since the client encrypts the
    // raw JSON text — not the parsed API shape).
    expect(EncryptedField.isEnvelope(body['name'] as String), isTrue);
    expect(await enc.decryptText(body['name'] as String), 'Morning Ride');

    // The polyline /meta never carries, from /track.
    expect(EncryptedField.isEnvelope(body['summary_polyline'] as String), isTrue);
    expect(await enc.decryptText(body['summary_polyline'] as String), 'abc123xyz');

    expect(jsonDecode(await enc.decryptText(body['start_latlng_json'] as String)),
        [48.0, 2.0]);
    expect(jsonDecode(await enc.decryptText(body['end_latlng_json'] as String)),
        [48.5, 2.5]);

    // The full profile from /track, not /meta's two-point downsample.
    final ep = jsonDecode(await enc.decryptText(body['elevation_profile_json'] as String))
        as Map<String, dynamic>;
    expect(ep['distances_km'], [0.0, 1.0, 2.0]);
    expect(ep['elevations_m'], [10.0, 20.0, 30.0]);
    // One envelope for both profile columns (R4-5).
    expect(body['elevation_profile_low_res_json'], body['elevation_profile_json']);

    // Compare-and-swap on the trip, by its name and the payload's version.
    expect(body['project'], 'Trip1');
    expect(body['lock_version'], 1);

    // Nothing listed is touched: no snapshot column is sent (let alone
    // nulled), and kudos_count is out of scope.
    expect(body.keys.where((k) => k.startsWith('original_')), isEmpty);
    expect(body.containsKey('kudos_count'), isFalse);
  });

  test('skips an activity with nothing in plain_fields (idempotent)', () async {
    final puts = <String, Map<String, dynamic>>{};
    final gets = <String>[];
    final mock = _server([_metaActivity(111, const [])], puts, gets);

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect((await EncryptionMigration(api, enc).run()).written, 0);
    expect(puts, isEmpty);
    expect(gets, ['/api/projects/Trip1/meta']);
  });

  test('a mixed plaintext/encrypted set of activities migrates only the plaintext one',
      () async {
    final puts = <String, Map<String, dynamic>>{};
    final gets = <String>[];
    final mock = _server(
        [_metaActivity(1, const ['name']), _metaActivity(2, const [])], puts, gets);

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect((await EncryptionMigration(api, enc).run()).written, 1);
    expect(puts.keys, ['/api/activities/1']);
    // Only the listed field is written.
    expect(puts['/api/activities/1']!.keys.toSet(), {'name', 'project', 'lock_version'});
  });
}
