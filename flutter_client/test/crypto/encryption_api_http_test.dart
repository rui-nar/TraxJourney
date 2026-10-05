import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/encryption_api_http.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

void main() {
  test('enable POSTs the payload to /api/encryption/enable', () async {
    late http.Request seen;
    final mock = MockClient((req) async {
      seen = req;
      return http.Response('', 201);
    });
    final api = HttpEncryptionApi(ApiClient(baseUrl: '', httpClient: mock));

    await api.enable({'device': {}, 'recovery': {'method': 'recovery_key'}});

    expect(seen.method, 'POST');
    expect(seen.url.path, '/api/encryption/enable');
    expect((jsonDecode(seen.body) as Map)['recovery']['method'], 'recovery_key');
  });

  test('fetchStatus encodes the device key and parses the response', () async {
    late Uri seenUrl;
    final mock = MockClient((req) async {
      seenUrl = req.url;
      return http.Response(
        jsonEncode({
          'enabled': true,
          'recovery_methods': ['qna'],
          'device': {
            'registered': true,
            'approved': true,
            'wrapped_cmk': 'WRAP',
            'ephemeral_public_key': 'EPH',
          },
        }),
        200,
      );
    });
    final api = HttpEncryptionApi(ApiClient(baseUrl: '', httpClient: mock));

    final status = await api.fetchStatus('PUB+/=KEY');

    expect(seenUrl.path, '/api/encryption/status');
    expect(seenUrl.queryParameters['device_public_key'], 'PUB+/=KEY');
    expect(status.enabled, isTrue);
    expect(status.recoveryMethods, ['qna']);
    expect(status.deviceApproved, isTrue);
    expect(status.wrappedCmkB64, 'WRAP');
    expect(status.ephemeralPublicKeyB64, 'EPH');
  });

  test('fetchStatus with no device key omits the query param', () async {
    late Uri seenUrl;
    final mock = MockClient((req) async {
      seenUrl = req.url;
      return http.Response(
        jsonEncode({
          'enabled': false,
          'recovery_methods': <String>[],
          'device': {'registered': false, 'approved': false},
        }),
        200,
      );
    });
    final api = HttpEncryptionApi(ApiClient(baseUrl: '', httpClient: mock));

    final status = await api.fetchStatus(null);

    expect(seenUrl.queryParameters.containsKey('device_public_key'), isFalse);
    expect(status.enabled, isFalse);
    expect(status.wrappedCmkB64, isNull);
  });

  group('recovery key confirm or replace (Decision 16)', () {
    HttpEncryptionApi apiAnswering(
            http.Response Function(http.Request) answer,
            List<http.Request> seen) =>
        HttpEncryptionApi(ApiClient(
            baseUrl: '',
            httpClient: MockClient((req) async {
              seen.add(req);
              return answer(req);
            })));

    test('enable returns the stored recovery wrap, or null from an older server',
        () async {
      final seen = <http.Request>[];
      final api = apiAnswering(
          (_) => http.Response(
              jsonEncode({'recovery_wrapped_cmk': 'STORED'}), 201),
          seen);
      expect(await api.enable({'recovery': {}}), 'STORED');

      final older = apiAnswering(
          (_) => http.Response(jsonEncode({'enabled': true}), 201), seen);
      expect(await older.enable({'recovery': {}}), isNull);
    });

    test('fetchStatus parses unconfirmed_recovery_methods', () async {
      final api = apiAnswering(
          (_) => http.Response(
              jsonEncode({
                'enabled': true,
                'recovery_methods': ['recovery_key'],
                'unconfirmed_recovery_methods': ['recovery_key'],
                'device': {'registered': false, 'approved': false},
              }),
              200),
          []);
      expect((await api.fetchStatus(null)).unconfirmedRecoveryMethods,
          ['recovery_key']);
    });

    test('a status from an older server parses as nothing unconfirmed, '
        'and unlock still works', () async {
      // A real device wrap, made by enabling against an in-memory server.
      final store = _Store();
      final enabling = _EnableCapture();
      await EncryptionService(store, enabling).enable(const RecoveryKeyChoice());
      final device = enabling.payload!['device'] as Map<String, dynamic>;

      final api = apiAnswering(
          (_) => http.Response(
              jsonEncode({
                'enabled': true,
                'recovery_methods': ['recovery_key'],
                'device': {
                  'registered': true,
                  'approved': true,
                  'wrapped_cmk': device['wrapped_cmk'],
                  'ephemeral_public_key': device['ephemeral_public_key'],
                },
              }),
              200),
          []);
      expect((await api.fetchStatus(null)).unconfirmedRecoveryMethods, isEmpty);

      final svc = EncryptionService(store, api);
      expect(await svc.unlock(), isTrue);
      expect(svc.needsRecoveryKeyReplacement, isFalse);
    });

    test('confirmRecovery POSTs the method and the wrap', () async {
      final seen = <http.Request>[];
      final api = apiAnswering((_) => http.Response('', 204), seen);

      await api.confirmRecovery('recovery_key', 'WRAP');

      expect(seen.single.method, 'POST');
      expect(seen.single.url.path, '/api/encryption/recovery/confirm');
      expect(jsonDecode(seen.single.body),
          {'method': 'recovery_key', 'wrapped_cmk': 'WRAP'});
    });

    test('confirmRecovery maps a 409 to RecoveryKeyConflict', () async {
      final api = apiAnswering((_) => http.Response('{}', 409), []);
      await expectLater(api.confirmRecovery('recovery_key', 'WRAP'),
          throwsA(isA<RecoveryKeyConflict>()));
    });

    test('confirmRecovery on an older server (405) is a plain failure',
        () async {
      final api = apiAnswering((_) => http.Response('{}', 405), []);
      await expectLater(api.confirmRecovery('recovery_key', 'WRAP'),
          throwsA(isA<ApiException>()
              .having((e) => e.statusCode, 'statusCode', 405)));
    });

    test('replaceRecoveryKey PUTs the wrap and returns the one written',
        () async {
      final seen = <http.Request>[];
      final api = apiAnswering(
          (_) => http.Response(
              jsonEncode({
                'method': 'recovery_key',
                'wrapped_cmk': 'WRITTEN',
                'salt': 'SALT',
                'kdf_params_json': null,
              }),
              200),
          seen);

      expect(await api.replaceRecoveryKey('WRAP', 'SALT'), 'WRITTEN');
      expect(seen.single.method, 'PUT');
      expect(seen.single.url.path, '/api/encryption/recovery/recovery_key');
      expect(jsonDecode(seen.single.body), {'wrapped_cmk': 'WRAP', 'salt': 'SALT'});
    });

    test('replaceRecoveryKey maps a 409 to RecoveryKeyConflict', () async {
      final api = apiAnswering((_) => http.Response('{}', 409), []);
      await expectLater(api.replaceRecoveryKey('WRAP', 'SALT'),
          throwsA(isA<RecoveryKeyConflict>()));
    });
  });
}

class _Store implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

/// Keeps the enable payload; every other call is unused here.
class _EnableCapture implements EncryptionApi {
  Map<String, dynamic>? payload;
  @override
  Future<void> enable(Map<String, dynamic> p) async => payload = p;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
