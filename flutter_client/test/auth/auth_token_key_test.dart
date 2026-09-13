// Guard for the persisted-JWT storage key (issue #151, ViewTrip ->
// TraxJourney rename). AuthService keeps the session token in
// SharedPreferences under a fixed key; renaming that key without migrating
// the old one silently signs every existing user out on their next launch.
//
// Every assertion below goes through [_tokenKey]. Moving to a new key is a
// deliberate edit here: change [_tokenKey], and add a test proving a token
// left under the legacy key is still restored (and moved) exactly once.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';

/// The key AuthService persists the JWT under today.
const _tokenKey = 'viewtrip_jwt';

void main() {
  setUp(() {
    // AuthService reads and writes the shared `api` singleton, so give every
    // test a fresh one whose login endpoint returns a known token.
    api = ApiClient(
      httpClient: MockClient((req) async => http.Response(
            jsonEncode({
              'access_token': 'jwt-from-server',
              'user': {'id': 1, 'email': 'a@example.com'},
            }),
            200,
          )),
    );
  });

  group('AuthService token storage key', () {
    test('restoreSession() restores a token stored under the key', () async {
      SharedPreferences.setMockInitialValues({_tokenKey: 'stored-jwt'});

      expect(await AuthService().restoreSession(), isTrue);
      expect(api.tokenForUpload, 'stored-jwt');
    });

    test('restoreSession() finds no session when the key is absent',
        () async {
      SharedPreferences.setMockInitialValues({});

      expect(await AuthService().restoreSession(), isFalse);
      expect(api.isAuthenticated, isFalse);
    });

    test('a login writes the token under the key', () async {
      SharedPreferences.setMockInitialValues({});

      await AuthService().loginWithPassword('a@example.com', 'pw');

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_tokenKey), 'jwt-from-server');
    });

    test('logout() removes the key', () async {
      SharedPreferences.setMockInitialValues({_tokenKey: 'stored-jwt'});
      final auth = AuthService();
      await auth.restoreSession();

      await auth.logout();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.containsKey(_tokenKey), isFalse);
      expect(api.isAuthenticated, isFalse);
    });
  });
}
