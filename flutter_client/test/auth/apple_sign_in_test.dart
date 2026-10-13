// Sign in with Apple (issue #580): when the login screen offers the button,
// what reaches Apple and the server on a sign-in (the raw nonce to the
// server, its SHA-256 to Apple), that cancelling is silent, that a server
// refusal shows, and the redirect URI web and Android hand Apple (R2-1).
// The plugin is faked through apple_sign_in.dart's requestAppleCredential;
// the server through a MockClient on the `api` global.

import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sign_in_with_apple/sign_in_with_apple.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/apple_sign_in.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/login_screen.dart';
import 'package:traxjourney_client/src/crypto/device_key_store.dart'
    show SecureKvStore;

/// What the fake plugin was asked for.
class _AppleCall {
  final String nonce;
  final WebAuthenticationOptions? options;
  _AppleCall(this.nonce, this.options);
}

const _credential = AuthorizationCredentialAppleID(
  userIdentifier: null,
  givenName: 'Ada',
  familyName: 'Lovelace',
  authorizationCode: 'the-code',
  email: null,
  identityToken: 'the-id-token',
  state: null,
);

/// What the iOS sheet returns on the first authorization: names and an id.
const _firstIos = AuthorizationCredentialAppleID(
  userIdentifier: 'apple-user-1',
  givenName: 'Ada',
  familyName: 'Lovelace',
  authorizationCode: 'the-code',
  email: null,
  identityToken: 'the-id-token',
  state: null,
);

/// What it returns on every later one: the id, no names.
const _laterIos = AuthorizationCredentialAppleID(
  userIdentifier: 'apple-user-1',
  givenName: null,
  familyName: null,
  authorizationCode: 'the-code',
  email: null,
  identityToken: 'the-id-token',
  state: null,
);

const _keptKey = 'apple_pending_name_v1_apple-user-1';

/// In-memory stand-in for the keystore that keeps Apple's name (R1-4).
class _MemoryKv implements SecureKvStore {
  final values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;

  @override
  Future<void> delete(String key) async => values.remove(key);
}

/// Pumps LoginScreen under a router, so a successful sign-in can navigate.
Future<void> _pumpLogin(WidgetTester tester) async {
  final router = GoRouter(
    initialLocation: '/login',
    routes: [
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      // A Scaffold, so a snackbar raised on the way out still shows.
      GoRoute(
          path: '/projects',
          builder: (_, __) => const Scaffold(body: Text('PROJECTS'))),
    ],
  );
  await tester.pumpWidget(
    ChangeNotifierProvider<AuthNotifier>(
      create: (_) => AuthNotifier(AuthService()),
      child: MaterialApp.router(routerConfig: router),
    ),
  );
  await tester.pumpAndSettle();
}

/// A widget test run as [platform]; the variant restores the platform.
void _testOn(TargetPlatform platform, String description,
        WidgetTesterCallback body) =>
    testWidgets(description, body,
        variant: TargetPlatformVariant.only(platform));

Future<void> _tapApple(WidgetTester tester) async {
  final button = find.byType(SignInWithAppleButton);
  await tester.ensureVisible(button);
  await tester.tap(button);
  await tester.pumpAndSettle();
}

void main() {
  late ApiClient originalApi;
  late String originalServicesId;
  late AppleCredentialRequester originalRequester;
  late SecureKvStore originalNameStore;
  late _MemoryKv kept;
  late List<http.Request> posts;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    // The encryption unlock after a sign-in reads the device key store.
    FlutterSecureStorage.setMockInitialValues({});
    originalApi = api;
    originalServicesId = appleServicesId;
    originalRequester = requestAppleCredential;
    originalNameStore = applePendingNameStore;
    kept = _MemoryKv();
    applePendingNameStore = kept;
    posts = [];
  });

  tearDown(() {
    api = originalApi;
    appleServicesId = originalServicesId;
    requestAppleCredential = originalRequester;
    applePendingNameStore = originalNameStore;
  });

  /// Answers POST /api/auth/apple with [status] and [body]; anything else
  /// (the encryption unlock after sign-in) with a 404.
  void serve(int status, Map<String, dynamic> body) {
    api = ApiClient(
      baseUrl: 'https://traxjourney.com',
      httpClient: MockClient((req) async {
        if (req.url.path == '/api/auth/apple') {
          posts.add(req);
          return http.Response(jsonEncode(body), status,
              headers: {'content-type': 'application/json'});
        }
        return http.Response('{"detail":"Not Found"}', 404);
      }),
    );
  }

  group('showAppleSignIn', () {
    test('iOS always shows it, with or without a Services ID', () {
      expect(
          showAppleSignIn(
              isWeb: false, platform: TargetPlatform.iOS, servicesId: ''),
          isTrue);
      expect(
          showAppleSignIn(
              isWeb: false, platform: TargetPlatform.iOS, servicesId: 'sid'),
          isTrue);
    });

    test('web and Android show it only with a Services ID', () {
      expect(showAppleSignIn(isWeb: true, servicesId: ''), isFalse);
      expect(showAppleSignIn(isWeb: true, servicesId: 'sid'), isTrue);
      expect(
          showAppleSignIn(
              isWeb: false, platform: TargetPlatform.android, servicesId: ''),
          isFalse);
      expect(
          showAppleSignIn(
              isWeb: false,
              platform: TargetPlatform.android,
              servicesId: 'sid'),
          isTrue);
    });

    test('a custom server hides it on web and Android, not on iOS', () {
      expect(
          showAppleSignIn(isWeb: true, servicesId: 'sid', customServer: true),
          isFalse);
      expect(
          showAppleSignIn(
              isWeb: false,
              platform: TargetPlatform.android,
              servicesId: 'sid',
              customServer: true),
          isFalse);
      expect(
          showAppleSignIn(
              isWeb: false,
              platform: TargetPlatform.iOS,
              servicesId: '',
              customServer: true),
          isTrue);
    });

    test('desktop never shows it', () {
      for (final p in [
        TargetPlatform.windows,
        TargetPlatform.linux,
        TargetPlatform.macOS,
      ]) {
        expect(showAppleSignIn(isWeb: false, platform: p, servicesId: 'sid'),
            isFalse,
            reason: '$p');
      }
    });
  });

  group('appleRedirectUri', () {
    test('an empty base takes the page origin, absolute', () {
      final uri = appleRedirectUri(
          '', Uri.parse('https://traxjourney.com/login?return_to=%2Fx'));
      expect(uri.toString(), 'https://traxjourney.com/api/auth/apple/callback');
      expect(uri.isAbsolute, isTrue);
    });

    test('a set base is used as is', () {
      final uri = appleRedirectUri(
          'https://val.traxjourney.com', Uri.parse('file:///ignored'));
      expect(uri.toString(),
          'https://val.traxjourney.com/api/auth/apple/callback');
    });
  });

  group('nonce', () {
    test('the raw nonce is 32 random bytes, base64url', () {
      final a = generateRawNonce();
      final b = generateRawNonce();
      expect(base64Url.decode(base64Url.normalize(a)), hasLength(32));
      expect(a, isNot(b));
    });

    test('the hash is lowercase hex SHA-256', () {
      expect(hashNonce('abc'),
          'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    });
  });

  group('isAppleSignInCancel', () {
    test('a native or Android cancel is a cancel', () {
      expect(
          isAppleSignInCancel(const SignInWithAppleAuthorizationException(
              code: AuthorizationErrorCode.canceled, message: 'x')),
          isTrue);
      expect(
          isAppleSignInCancel(const SignInWithAppleAuthorizationException(
              code: AuthorizationErrorCode.failed, message: 'x')),
          isFalse);
    });

    test('a closed Apple JS popup is a cancel; other web errors are not', () {
      expect(
          isAppleSignInCancel(const SignInWithAppleCredentialsException(
              message: 'Authentication failed with popup_closed_by_user')),
          isTrue);
      expect(
          isAppleSignInCancel(const SignInWithAppleCredentialsException(
              message: 'Authentication failed with UNKNOWN_SIWA_ERROR')),
          isFalse);
    });
  });

  group('LoginScreen', () {
    _testOn(TargetPlatform.android,
        'Android with a Services ID shows the Apple button', (tester) async {
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsOneWidget);
      expect(find.text('Continue with Apple'), findsOneWidget);
    });

    _testOn(TargetPlatform.android,
        'Android without a Services ID shows no Apple button', (tester) async {
      appleServicesId = '';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsNothing);
    });

    _testOn(
        TargetPlatform.iOS, 'iOS shows the Apple button without a Services ID',
        (tester) async {
      appleServicesId = '';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsOneWidget);
    });

    _testOn(
        TargetPlatform.android,
        'a sign-in sends Apple the hash and the server the raw nonce, '
        'then opens the projects', (tester) async {
      appleServicesId = 'com.traxjourney.web';
      serve(200, {
        'access_token': 'jwt',
        'token_type': 'bearer',
        'user': {'id': 7, 'email': 'a@privaterelay.appleid.com'},
      });
      final calls = <_AppleCall>[];
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) async {
        calls.add(_AppleCall(nonce, webAuthenticationOptions));
        return _credential;
      };
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(calls, hasLength(1));
      expect(posts, hasLength(1));
      final body = jsonDecode(posts.single.body) as Map<String, dynamic>;
      expect(body['id_token'], 'the-id-token');
      expect(body['authorization_code'], 'the-code');
      expect(body['given_name'], 'Ada');
      expect(body['family_name'], 'Lovelace');
      final raw = body['nonce'] as String;
      expect(raw, isNotEmpty);
      expect(calls.single.nonce, sha256.convert(utf8.encode(raw)).toString());
      expect(calls.single.nonce, isNot(raw));

      // Android goes through Apple's web flow, back through this server.
      expect(calls.single.options!.clientId, 'com.traxjourney.web');
      expect(calls.single.options!.redirectUri.toString(),
          'https://traxjourney.com/api/auth/apple/callback');

      expect(find.text('PROJECTS'), findsOneWidget);
    });

    _testOn(TargetPlatform.iOS, 'iOS uses the native sheet: no web options',
        (tester) async {
      serve(200, {
        'access_token': 'jwt',
        'token_type': 'bearer',
        'user': {'id': 7},
      });
      final calls = <_AppleCall>[];
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) async {
        calls.add(_AppleCall(nonce, webAuthenticationOptions));
        return _credential;
      };
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(calls.single.options, isNull);
      expect(posts, hasLength(1));
    });

    _testOn(
        TargetPlatform.android, 'cancelling shows nothing and posts nothing',
        (tester) async {
      appleServicesId = 'com.traxjourney.web';
      serve(200, {});
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) async {
        throw const SignInWithAppleAuthorizationException(
            code: AuthorizationErrorCode.canceled, message: 'canceled');
      };
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(posts, isEmpty);
      expect(find.byType(SnackBar), findsNothing);
      expect(find.byIcon(Icons.error_outline), findsNothing);
      expect(find.byType(LoginScreen), findsOneWidget);
    });

    _testOn(TargetPlatform.android, 'a failed Apple flow shows an error',
        (tester) async {
      appleServicesId = 'com.traxjourney.web';
      serve(200, {});
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) async {
        throw const SignInWithAppleAuthorizationException(
            code: AuthorizationErrorCode.failed, message: 'failed');
      };
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(posts, isEmpty);
      expect(find.textContaining('Apple sign-in failed'), findsOneWidget);
    });

    // Android: closing the Custom Tab leaves the attempt pending; the plugin
    // rejects it when the next one starts. That rejection is not an error.
    _testOn(TargetPlatform.android,
        'a retry after a closed Custom Tab shows no error for the first try',
        (tester) async {
      appleServicesId = 'com.traxjourney.web';
      serve(200, {
        'access_token': 'jwt',
        'token_type': 'bearer',
        'user': {'id': 7},
      });
      final first = Completer<AuthorizationCredentialAppleID>();
      var calls = 0;
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) async {
        if (++calls == 1) return first.future;
        first.completeError(const SignInWithAppleAuthorizationException(
            code: AuthorizationErrorCode.unknown, message: 'superseded'));
        return _credential;
      };
      await _pumpLogin(tester);

      await _tapApple(tester);
      await _tapApple(tester);

      expect(find.textContaining('Apple sign-in failed'), findsNothing);
      expect(posts, hasLength(1));
      expect(find.text('PROJECTS'), findsOneWidget);
    });

    for (final (status, detail) in [
      (401, 'Invalid Apple id_token'),
      (502, 'Apple sign-in unavailable'),
    ]) {
      _testOn(
          TargetPlatform.android, 'a $status from the server shows "$detail"',
          (tester) async {
        appleServicesId = 'com.traxjourney.web';
        serve(status, {'detail': detail});
        requestAppleCredential = (
                {required String nonce,
                WebAuthenticationOptions? webAuthenticationOptions}) async =>
            _credential;
        await _pumpLogin(tester);

        await _tapApple(tester);

        expect(posts, hasLength(1));
        expect(find.text(detail), findsOneWidget);
        expect(find.byType(LoginScreen), findsOneWidget);
        expect(api.isAuthenticated, isFalse);
      });
    }
  });

  // R1-4: Apple sends the name on the first authorization only. A first
  // sign-in that fails must not lose it for the retry.
  group('kept name', () {
    const ok = {
      'access_token': 'jwt',
      'token_type': 'bearer',
      'user': {'id': 7},
    };
    Map<String, dynamic> bodyOf(http.Request r) =>
        jsonDecode(r.body) as Map<String, dynamic>;
    void appleReturns(List<AuthorizationCredentialAppleID> answers) {
      var i = 0;
      requestAppleCredential = (
              {required String nonce,
              WebAuthenticationOptions? webAuthenticationOptions}) async =>
          answers[i++];
    }

    _testOn(
        TargetPlatform.iOS,
        'a failed first sign-in keeps the name, the nameless retry sends it, '
        'and the success forgets it', (tester) async {
      appleReturns([_firstIos, _laterIos]);
      serve(502, {'detail': 'Apple sign-in unavailable'});
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(posts, hasLength(1));
      expect(bodyOf(posts[0])['given_name'], 'Ada');
      expect(jsonDecode(kept.values[_keptKey]!),
          {'given_name': 'Ada', 'family_name': 'Lovelace'});
      expect(find.byType(LoginScreen), findsOneWidget);

      serve(200, ok);
      await _tapApple(tester);

      expect(posts, hasLength(2));
      expect(bodyOf(posts[1])['given_name'], 'Ada');
      expect(bodyOf(posts[1])['family_name'], 'Lovelace');
      expect(kept.values, isEmpty);
      expect(find.text('PROJECTS'), findsOneWidget);
    });

    _testOn(TargetPlatform.iOS, 'names Apple sends replace the kept ones',
        (tester) async {
      kept.values[_keptKey] =
          jsonEncode({'given_name': 'Old', 'family_name': 'Name'});
      appleReturns([_firstIos]);
      serve(502, {'detail': 'Apple sign-in unavailable'});
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(bodyOf(posts.single)['given_name'], 'Ada');
      expect(bodyOf(posts.single)['family_name'], 'Lovelace');
      expect(jsonDecode(kept.values[_keptKey]!),
          {'given_name': 'Ada', 'family_name': 'Lovelace'});
    });

    _testOn(TargetPlatform.iOS,
        'a nameless sign-in with nothing kept sends no name', (tester) async {
      appleReturns([_laterIos]);
      serve(200, ok);
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(bodyOf(posts.single)['given_name'], isNull);
      expect(bodyOf(posts.single)['family_name'], isNull);
      expect(find.text('PROJECTS'), findsOneWidget);
    });

    // Web and Android credentials carry no userIdentifier: nothing to key on.
    _testOn(TargetPlatform.android, 'without a userIdentifier nothing is kept',
        (tester) async {
      appleServicesId = 'com.traxjourney.web';
      appleReturns([
        _credential,
        const AuthorizationCredentialAppleID(
          userIdentifier: null,
          givenName: null,
          familyName: null,
          authorizationCode: 'the-code',
          email: null,
          identityToken: 'the-id-token',
          state: null,
        ),
      ]);
      serve(502, {'detail': 'Apple sign-in unavailable'});
      await _pumpLogin(tester);

      await _tapApple(tester);

      expect(bodyOf(posts.single)['given_name'], 'Ada');
      expect(kept.values, isEmpty);

      serve(200, ok);
      await _tapApple(tester);

      expect(posts, hasLength(2));
      expect(bodyOf(posts[1])['given_name'], isNull);
      expect(kept.values, isEmpty);
      expect(find.text('PROJECTS'), findsOneWidget);
    });
  });
}
