// Guard for the persisted-JWT storage key (issue #151, the TraxJourney
// rename). AuthService keeps the session token in
// SharedPreferences under a fixed key; renaming that key without migrating
// the old one silently signs every existing user out on their next launch.
//
// Every assertion below goes through [_tokenKey]. The key moved from
// [_legacyTokenKey] in #151, so the migration group proves a token left under
// the legacy key is still restored, moved exactly once, and cleared on logout.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';

/// The key AuthService persists the JWT under today.
const _tokenKey = 'traxjourney_jwt';

/// The key it used before the TraxJourney rename (issue #151).
const _legacyTokenKey = 'viewtrip_jwt';

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

  group('migration from the pre-rename key', () {
    test('a token only under the legacy key is restored and moved', () async {
      SharedPreferences.setMockInitialValues({_legacyTokenKey: 'legacy-jwt'});

      expect(await AuthService().restoreSession(), isTrue);
      expect(api.tokenForUpload, 'legacy-jwt');
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_tokenKey), 'legacy-jwt');
    });

    test('the legacy key is deleted once migrated', () async {
      SharedPreferences.setMockInitialValues({_legacyTokenKey: 'legacy-jwt'});

      await AuthService().restoreSession();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.containsKey(_legacyTokenKey), isFalse);
    });

    test('the new key wins when both are present', () async {
      SharedPreferences.setMockInitialValues(
          {_tokenKey: 'new-jwt', _legacyTokenKey: 'legacy-jwt'});

      expect(await AuthService().restoreSession(), isTrue);
      expect(api.tokenForUpload, 'new-jwt');
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(_tokenKey), 'new-jwt');
    });

    test('logout() clears both keys', () async {
      SharedPreferences.setMockInitialValues(
          {_tokenKey: 'new-jwt', _legacyTokenKey: 'legacy-jwt'});

      await AuthService().logout();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.containsKey(_tokenKey), isFalse);
      expect(prefs.containsKey(_legacyTokenKey), isFalse);
    });
  });
}
