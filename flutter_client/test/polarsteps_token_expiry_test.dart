import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/polarsteps_import_notifier.dart';

/// Builds a notifier whose HTTP layer is driven by [handler].
PolarstepsImportNotifier _notifier(
    Future<http.Response> Function(http.Request) handler) {
  final mock = MockClient((req) => handler(req));
  final client = ApiClient(httpClient: mock)..setToken('jwt');
  return PolarstepsImportNotifier(client: client);
}

http.Response _json(int status, Object body) =>
    http.Response(jsonEncode(body), status,
        headers: {'content-type': 'application/json'});

void main() {
  group('Polarsteps token expiry', () {
    test('loadTrips: a Polarsteps 401 sets tokenExpired, not a raw error', () async {
      final n = _notifier((req) async =>
          _json(401, {'detail': 'Polarsteps token expired'}));

      await n.loadTrips();

      expect(n.tokenExpired, isTrue);
      expect(n.error, isNull);
      expect(n.polarstepsNotConnected, isFalse);
    });

    test('loadTrips: an app-JWT 401 is NOT treated as Polarsteps expiry', () async {
      final n = _notifier((req) async => _json(401, {'detail': 'Token expired'}));

      await n.loadTrips();

      expect(n.tokenExpired, isFalse);
      expect(n.error, isNotNull); // falls through to generic error handling
    });

    test('reconnect: good token clears expiry and resumes the failed call', () async {
      var connected = false;
      final n = _notifier((req) async {
        if (req.url.path == '/api/polarsteps/connect') {
          connected = true;
          return _json(200, {'connected': true, 'username': 'alice'});
        }
        if (req.url.path == '/api/polarsteps/trips') {
          // Fail until reconnected, then succeed → proves resume re-ran it.
          if (!connected) return _json(401, {'detail': 'Polarsteps token expired'});
          return _json(200, [
            {'id': 1, 'name': 'Trip', 'steps_count': 0}
          ]);
        }
        return _json(404, {'detail': 'unexpected ${req.url.path}'});
      });

      await n.loadTrips();
      expect(n.tokenExpired, isTrue);

      final ok = await n.reconnect('1|fresh');

      expect(ok, isTrue);
      expect(n.tokenExpired, isFalse);
      expect(n.reconnecting, isFalse);
      expect(n.trips, hasLength(1));
      expect(n.error, isNull);
    });

    test('reconnect: bad token keeps the panel up with an error', () async {
      final n = _notifier((req) async {
        if (req.url.path == '/api/polarsteps/connect') {
          return _json(401, {'detail': 'Invalid Polarsteps token — please check and try again'});
        }
        return _json(401, {'detail': 'Polarsteps token expired'});
      });

      await n.loadTrips();
      expect(n.tokenExpired, isTrue);

      final ok = await n.reconnect('1|bad');

      expect(ok, isFalse);
      expect(n.tokenExpired, isTrue); // still expired — user stays on the panel
      expect(n.reconnecting, isFalse);
      expect(n.error, contains('Invalid Polarsteps token'));
    });
  });
}
