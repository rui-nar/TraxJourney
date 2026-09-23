import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_migration.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

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
  test('encrypts plaintext memory/journal, skips already-encrypted', () async {
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
            'items': [
              {
                'item_type': 'memory',
                'memory': {
                  'id': 1, 'name': 'Beach', 'description': 'Sunny day',
                  'date': '2025-01-01', 'geo_mode': 'start_of_day',
                },
              },
              {
                'item_type': 'memory',
                'memory': {
                  'id': 2, 'name': null,
                  'description': 'v1.AAAA.BBBB', // already an envelope -> skip
                  'date': '2025-01-01', 'geo_mode': 'start_of_day',
                },
              },
              {
                'item_type': 'journal',
                'journal': {
                  'id': 5, 'description': 'Private thoughts',
                  'date': '2025-01-02', 'geo_mode': 'start_of_day',
                },
              },
            ],
          }),
          200,
        );
      }
      if (req.method == 'PUT') {
        puts[path] = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response('', 204);
      }
      return http.Response('not found', 404);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice()); // unlock

    final migrated = await EncryptionMigration(api, enc).run();

    expect(migrated, 2); // memory 1 + journal 5; memory 2 skipped
    expect(puts.keys.toSet(), {'/api/memories/1', '/api/journal/5'});

    // The written values are ciphertext envelopes, and decrypt back correctly.
    final mem = puts['/api/memories/1']!;
    expect(EncryptedField.isEnvelope(mem['name'] as String), isTrue);
    expect(EncryptedField.isEnvelope(mem['description'] as String), isTrue);
    expect(await enc.decryptText(mem['name'] as String), 'Beach');
    expect(await enc.decryptText(mem['description'] as String), 'Sunny day');
    expect(mem['date'], '2025-01-01'); // non-encrypted fields preserved

    final jrn = puts['/api/journal/5']!;
    expect(await enc.decryptText(jrn['description'] as String), 'Private thoughts');
  });

  test('re-running is a no-op once everything is encrypted (resumable)', () async {
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
            'items': [
              {
                'item_type': 'memory',
                'memory': {
                  'id': 1, 'name': 'v1.AA.BB', 'description': 'v1.CC.DD',
                  'date': '2025-01-01', 'geo_mode': 'start_of_day',
                },
              },
            ],
          }),
          200,
        );
      }
      putCount++;
      return http.Response('', 204);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect(await EncryptionMigration(api, enc).run(), 0);
    expect(putCount, 0);
  });

  test('skips a project shared with the caller (role: editor — issue #106)',
      () async {
    // A companion's own encryption migration must only touch their own
    // projects — key-sharing with the owner of a shared project is out of
    // scope, and GETting a shared project's name without ?owner= would 404
    // (or worse, hit an unrelated same-named project of the caller's own).
    final getPaths = <String>[];
    final mock = MockClient((req) async {
      if (req.method == 'GET' && req.url.path == '/api/projects/') {
        return http.Response(jsonEncode([
          {'name': 'OwnTrip'},
          {
            'name': 'FriendTrip',
            'owner_id': 7,
            'owner_name': 'Bob',
            'role': 'editor',
          },
        ]), 200);
      }
      getPaths.add(req.url.path);
      if (req.method == 'GET') {
        return http.Response(jsonEncode({'items': <dynamic>[]}), 200);
      }
      return http.Response('', 204);
    });

    final api = ApiClient(baseUrl: '', httpClient: mock);
    final enc = EncryptionService(_FakeStore(), _FakeApi());
    await enc.enable(const RecoveryKeyChoice());

    expect(await EncryptionMigration(api, enc).run(), 0);
    expect(getPaths, ['/api/projects/OwnTrip']);
  });
}
