import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_migration.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

/// Pins the end-to-end encryption coverage documented in docs/ENCRYPTION.md
/// (issue #433): for each resource the migration writes, the set of fields
/// that leave the device as ciphertext must be exactly
/// [encryptedFieldsByResource], and the set that leaves as plaintext must be
/// exactly the list below. A field added to (or dropped from) either side of
/// the migration fails here until the constant and the doc are updated too.
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

/// Plaintext fields the migration re-sends untouched, per resource. Listed
/// explicitly (not derived) so the doc's "stays plaintext" table has the same
/// pin as its "encrypted" table.
const _plaintextFieldsByResource = <String, Set<String>>{
  'memory': {'date', 'geo_mode', 'time', 'lat', 'lon'},
  'journal': {'date', 'geo_mode', 'time', 'lat', 'lon'},
  // The two edit-undo snapshot columns are scrubbed to null, not encrypted.
  'activity': {'original_polyline', 'original_elevation_profile_json'},
};

/// A project whose one memory, one journal entry and one activity carry every
/// field the API can hand back, all still plaintext.
const _projectDetails = <String, dynamic>{
  'items': [
    {
      'item_type': 'memory',
      'memory': {
        'id': 1,
        'public_id': 'abc',
        'name': 'Beach',
        'description': 'Sunny day',
        'date': '2025-01-01',
        'time': '14:30',
        'geo_mode': 'custom',
        'lat': 43.1,
        'lon': 5.9,
        'photos': ['p1'],
        'comment_count': 2,
        'like_count': 3,
      },
    },
    {
      'item_type': 'journal',
      'journal': {
        'id': 2,
        'description': 'Private thoughts',
        'date': '2025-01-02',
        'time': '21:00',
        'geo_mode': 'custom',
        'lat': 44.0,
        'lon': 6.0,
        'photos': ['p2'],
      },
    },
  ],
  'activities': [
    {
      'id': 111,
      'name': 'Morning Ride',
      'type': 'Ride',
      'distance': 42000.0,
      'moving_time': 3600,
      'elapsed_time': 4000,
      'total_elevation_gain': 500.0,
      'start_date': '2025-01-01T08:00:00Z',
      'start_date_local': '2025-01-01T09:00:00',
      'timezone': 'Europe/Paris',
      'kudos_count': 4,
      'average_speed': 11.6,
      'average_heartrate': 140.0,
      'elev_high': 900.0,
      'elev_low': 400.0,
      'source': 'gpx',
      'map': {'summary_polyline': 'abc123xyz'},
      'start_latlng': [48.0, 2.0],
      'end_latlng': [48.5, 2.5],
      'elevation_profile': [
        [0.0, 10.0],
        [1.0, 20.0],
      ],
    },
  ],
};

void main() {
  test('the migration encrypts exactly encryptedFieldsByResource and nothing else',
      () async {
    final puts = <String, Map<String, dynamic>>{};
    final mock = MockClient((req) async {
      final path = req.url.path;
      if (req.method == 'GET' && path == '/api/projects/') {
        return http.Response(jsonEncode([{'name': 'Trip1'}]), 200);
      }
      if (req.method == 'GET' && path == '/api/projects/Trip1') {
        return http.Response(jsonEncode(_projectDetails), 200);
      }
      if (req.method == 'PUT') {
        puts[path] = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response('', 200);
      }
      return http.Response('not found', 404);
    });

    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());
    final migrated =
        await EncryptionMigration(ApiClient(baseUrl: '', httpClient: mock), enc)
            .run();
    expect(migrated, 3);

    const pathByResource = {
      'memory': '/api/memories/1',
      'journal': '/api/journal/2',
      'activity': '/api/activities/111',
    };
    expect(pathByResource.keys.toSet(), encryptedFieldsByResource.keys.toSet(),
        reason: 'every resource in the constant must be exercised here');
    expect(pathByResource.keys.toSet(), _plaintextFieldsByResource.keys.toSet());

    for (final entry in pathByResource.entries) {
      final resource = entry.key;
      final body = puts[entry.value];
      expect(body, isNotNull, reason: 'no PUT for $resource');

      final ciphertext = <String>{};
      final plaintext = <String>{};
      for (final field in body!.entries) {
        final v = field.value;
        (v is String && EncryptedField.isEnvelope(v) ? ciphertext : plaintext)
            .add(field.key);
      }
      expect(ciphertext, encryptedFieldsByResource[resource],
          reason: '$resource: encrypted fields drifted from '
              'encryptedFieldsByResource — update it and docs/ENCRYPTION.md');
      expect(plaintext, _plaintextFieldsByResource[resource],
          reason: '$resource: plaintext fields drifted — update '
              '_plaintextFieldsByResource and docs/ENCRYPTION.md');
    }
  });

  test('a still-plaintext value the migration does not know is never sent', () {
    // A tripwire for the doc's "stays plaintext" list: these are fields the
    // API returns that the migration deliberately leaves alone (server-owned
    // metadata). They must not appear in encryptedFieldsByResource.
    const serverOwned = {
      'date', 'time', 'lat', 'lon', 'geo_mode', 'photos', 'public_id',
      'type', 'distance', 'moving_time', 'elapsed_time', 'total_elevation_gain',
      'start_date', 'start_date_local', 'timezone', 'kudos_count',
      'average_speed', 'average_heartrate', 'elev_high', 'elev_low', 'source',
    };
    for (final fields in encryptedFieldsByResource.values) {
      expect(fields.intersection(serverOwned), isEmpty);
    }
  });
}
