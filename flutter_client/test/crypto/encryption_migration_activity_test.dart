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
/// (issue #29): EncryptionMigration.run() must also encrypt a still-plaintext
/// activity's name/summary_polyline/start_latlng/end_latlng/elevation_profile
/// via PUT /api/activities/{id}, skip an already-encrypted one (idempotent),
/// and leave non-in-scope fields (e.g. kudos_count) untouched.
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

void main() {
  test('encrypts a plaintext activity\'s in-scope fields via PUT /api/activities/{id}',
      () async {
    final puts = <String, Map<String, dynamic>>{};

    final mock = MockClient((req) async {
      final path = req.url.path;
      if (req.method == 'GET' && path == '/api/projects/') {
        return http.Response(jsonEncode([
          {'name': 'Trip1'}
        ]), 200);
      }
      if (req.method == 'GET' && path == '/api/projects/Trip1') {
        return http.Response(
          jsonEncode({
            'items': const [],
            'activities': [
              {
                'id': 111,
                'name': 'Morning Ride',
                'kudos_count': 4,
                'map': {'summary_polyline': 'abc123xyz'},
                'start_latlng': [48.0, 2.0],
                'end_latlng': [48.5, 2.5],
                'elevation_profile': [
                  [0.0, 10.0],
                  [1.0, 20.0],
                ],
              },
            ],
          }),
          200,
        );
      }
      if (req.method == 'PUT') {
        puts[path] = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response('', 200);
      }
      return http.Response('not found', 404);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    final migrated = await EncryptionMigration(api, enc).run();

    expect(migrated, 1);
    expect(puts.keys, contains('/api/activities/111'));
    final body = puts['/api/activities/111']!;

    // In-scope fields are now ciphertext envelopes that decrypt back to the
    // exact plaintext (name/summary_polyline directly; the geometry fields via
    // the same JSON shape the DB column stores, since the client encrypts the
    // raw JSON text — not the parsed API shape).
    expect(EncryptedField.isEnvelope(body['name'] as String), isTrue);
    expect(await enc.decryptText(body['name'] as String), 'Morning Ride');

    expect(EncryptedField.isEnvelope(body['summary_polyline'] as String), isTrue);
    expect(await enc.decryptText(body['summary_polyline'] as String), 'abc123xyz');

    expect(EncryptedField.isEnvelope(body['start_latlng_json'] as String), isTrue);
    expect(jsonDecode(await enc.decryptText(body['start_latlng_json'] as String)),
        [48.0, 2.0]);

    expect(EncryptedField.isEnvelope(body['end_latlng_json'] as String), isTrue);
    expect(jsonDecode(await enc.decryptText(body['end_latlng_json'] as String)),
        [48.5, 2.5]);

    expect(EncryptedField.isEnvelope(body['elevation_profile_json'] as String), isTrue);
    final ep = jsonDecode(await enc.decryptText(body['elevation_profile_json'] as String))
        as Map<String, dynamic>;
    expect(ep['distances_km'], [0.0, 1.0]);
    expect(ep['elevations_m'], [10.0, 20.0]);

    // Reused for the low-res column too (see _migrateActivity's doc comment).
    expect(body['elevation_profile_low_res_json'], body['elevation_profile_json']);

    // Edit-undo snapshot columns are scrubbed (best-effort, DB-only, unreadable
    // by the client) rather than left as a potential plaintext remnant.
    expect(body.containsKey('original_polyline'), isTrue);
    expect(body['original_polyline'], isNull);
    expect(body.containsKey('original_elevation_profile_json'), isTrue);
    expect(body['original_elevation_profile_json'], isNull);

    // kudos_count is out of scope — never sent in this narrow field update.
    expect(body.containsKey('kudos_count'), isFalse);
  });

  test('skips an activity whose fields are already encrypted (idempotent)', () async {
    var putCount = 0;
    final mock = MockClient((req) async {
      if (req.method == 'GET' && req.url.path == '/api/projects/') {
        return http.Response(jsonEncode([
          {'name': 'Trip1'}
        ]), 200);
      }
      if (req.method == 'GET') {
        return http.Response(
          jsonEncode({
            'items': const [],
            'activities': [
              {
                'id': 111,
                'name': 'v1.AA.BB',
                'map': {'summary_polyline': 'v1.CC.DD'},
                // Once encrypted, the server nulls the parsed geometry fields
                // and carries ciphertext via the sibling *_enc keys instead —
                // simulate exactly that shape here.
                'start_latlng': null,
                'end_latlng': null,
                'elevation_profile': null,
                'start_latlng_enc': 'v1.EE.FF',
                'end_latlng_enc': 'v1.GG.HH',
                'elevation_profile_enc': 'v1.II.JJ',
              },
            ],
          }),
          200,
        );
      }
      putCount++;
      return http.Response('', 200);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect(await EncryptionMigration(api, enc).run(), 0);
    expect(putCount, 0);
  });

  test('a mixed plaintext/encrypted set of activities migrates only the plaintext one',
      () async {
    final puts = <String>[];
    final mock = MockClient((req) async {
      if (req.method == 'GET' && req.url.path == '/api/projects/') {
        return http.Response(jsonEncode([
          {'name': 'Trip1'}
        ]), 200);
      }
      if (req.method == 'GET') {
        return http.Response(
          jsonEncode({
            'items': const [],
            'activities': [
              {
                'id': 1, 'name': 'Plain', 'map': {'summary_polyline': 'poly1'},
              },
              {
                'id': 2, 'name': 'v1.AA.BB', 'map': {'summary_polyline': 'v1.CC.DD'},
              },
            ],
          }),
          200,
        );
      }
      puts.add(req.url.path);
      return http.Response('', 200);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect(await EncryptionMigration(api, enc).run(), 1);
    expect(puts, ['/api/activities/1']);
  });
}
