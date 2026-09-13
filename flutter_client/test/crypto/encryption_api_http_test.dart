import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/encryption_api_http.dart';

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
}
