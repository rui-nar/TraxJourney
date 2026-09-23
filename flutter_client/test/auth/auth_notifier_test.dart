// Tests for AuthNotifier.init()'s local JWT-expiry gate (load-performance
// audit fix): a warm relaunch with a token that has comfortably more than
// a few minutes left no longer blocks app start on /api/auth/me — the
// session is restored immediately from the persisted token, and getMe()
// still runs in the background to catch revocation / a forced 401.
//
// Covers:
//  - a token far from expiry restores immediately, without init() waiting
//    on getMe() to resolve;
//  - an expired/near-expiry token keeps the original blocking behavior;
//  - a background 401 (discovered after init() already returned) still
//    forces logout.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/api/client.dart';

/// Builds an unsigned (test-only) JWT carrying just an `exp` claim. Nothing
/// in AuthNotifier verifies the signature client-side — only the payload's
/// `exp` is read — so a dummy signature segment is fine here.
String _fakeJwt(DateTime exp) {
  String seg(Object payload) =>
      base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
  final header = seg({'alg': 'none', 'typ': 'JWT'});
  final body = seg({'exp': exp.millisecondsSinceEpoch ~/ 1000});
  return '$header.$body.sig';
}

Map<String, dynamic> _userMap({String email = 'restored@example.com'}) => {
      'id': '1',
      'email': email,
      'display_name': 'Restored User',
      'avatar_url': '',
      'auth_provider': 'local',
    };

/// Fake AuthService: restoreSession() plants [token] on the real (global)
/// `api` singleton, exactly like the real AuthService does, so
/// AuthNotifier's `api.tokenForUpload` read sees it. getMe()/logout() are
/// driven explicitly per test.
class _FakeAuthService extends AuthService {
  _FakeAuthService(this.token,
      {Future<Map<String, dynamic>>? getMeFuture, Object? getMeError})
      : _getMeFuture = getMeFuture,
        _getMeError = getMeError;

  final String token;
  final Future<Map<String, dynamic>>? _getMeFuture;
  // Thrown lazily from getMe() itself, rather than passed as a pre-built
  // Future.error(...): building that eagerly, before init() ever awaits it,
  // trips Dart's unhandled-error zone reporting and fails the test spuriously.
  final Object? _getMeError;

  int getMeCalls = 0;
  bool loggedOut = false;
  final List<String> appOpenedCalls = [];

  @override
  Future<bool> restoreSession() async {
    api.setToken(token);
    return true;
  }

  @override
  Future<void> appOpened(String sessionState) async {
    appOpenedCalls.add(sessionState);
  }

  @override
  Future<Map<String, dynamic>> getMe() async {
    getMeCalls++;
    final error = _getMeError;
    if (error != null) throw error;
    // No future supplied means "never resolves" — used to prove init()
    // did not await it.
    return _getMeFuture ?? Completer<Map<String, dynamic>>().future;
  }

  @override
  Future<void> logout() async {
    loggedOut = true;
    api.clearToken();
  }
}

/// Fake AuthService with no persisted token at all — restoreSession()
/// returns false, as it does for a first-ever launch or after a logout.
class _FakeNoSessionAuthService extends AuthService {
  final List<String> appOpenedCalls = [];

  @override
  Future<bool> restoreSession() async => false;

  @override
  Future<void> appOpened(String sessionState) async {
    appOpenedCalls.add(sessionState);
  }
}

void main() {
  setUp(() {
    // Each AuthNotifier under test reads/writes the shared `api` singleton
    // (as production code does), so start every test from a clean slate.
    api = ApiClient();
  });

  test(
      'a token far from expiry restores immediately without blocking on getMe()',
      () async {
    final token = _fakeJwt(DateTime.now().toUtc().add(const Duration(days: 3)));
    // getMe() is never supplied a resolving future: if init() awaited it,
    // this test would hang and time out instead of completing.
    final service = _FakeAuthService(token);
    final notifier = AuthNotifier(service);

    await notifier.init();

    expect(notifier.isRestoring, isFalse);
    expect(notifier.isLoading, isFalse);
    expect(notifier.user, isNotNull,
        reason: 'restored optimistically from the local token');
    expect(notifier.user!.id, isEmpty,
        reason: 'the User.restored sentinel, not yet the real profile');
    expect(service.getMeCalls, 1,
        reason: 'getMe() is still fired — just not blocked on');
    expect(service.appOpenedCalls, ['resumed'],
        reason: 'an optimistic restore counts as a returning visit');
  });

  test('an expired token keeps the original blocking behavior', () async {
    final token =
        _fakeJwt(DateTime.now().toUtc().subtract(const Duration(minutes: 1)));
    final service =
        _FakeAuthService(token, getMeFuture: Future.value(_userMap()));
    final notifier = AuthNotifier(service);

    await notifier.init();

    expect(notifier.isRestoring, isFalse);
    expect(service.getMeCalls, 1);
    expect(notifier.user?.email, 'restored@example.com',
        reason: 'init() awaited getMe() and used the real profile it '
            'returned, not the sentinel');
    expect(service.appOpenedCalls, ['resumed'],
        reason: 'a blocking getMe() that succeeds is still a resumed session');
  });

  test('a token near expiry (inside the buffer) also blocks as before',
      () async {
    final token =
        _fakeJwt(DateTime.now().toUtc().add(const Duration(minutes: 1)));
    final service =
        _FakeAuthService(token, getMeFuture: Future.value(_userMap()));
    final notifier = AuthNotifier(service);

    await notifier.init();

    expect(notifier.user?.email, 'restored@example.com');
  });

  test('a background 401 still forces logout after an optimistic restore',
      () async {
    final token = _fakeJwt(DateTime.now().toUtc().add(const Duration(days: 3)));
    final meCompleter = Completer<Map<String, dynamic>>();
    final service = _FakeAuthService(token, getMeFuture: meCompleter.future);
    final notifier = AuthNotifier(service);

    await notifier.init();
    expect(notifier.user, isNotNull,
        reason: 'optimistically restored before the background check lands');

    meCompleter.completeError(ApiException(401, '{"detail":"expired"}'));
    // Let the background continuation (await getMe() -> await logout() ->
    // notifyListeners()) run to completion.
    await Future<void>.delayed(const Duration(milliseconds: 10));

    expect(notifier.user, isNull);
    expect(service.loggedOut, isTrue);
  });

  test(
      'a background non-401 failure keeps the optimistically-restored session',
      () async {
    final token = _fakeJwt(DateTime.now().toUtc().add(const Duration(days: 3)));
    final meCompleter = Completer<Map<String, dynamic>>();
    final service = _FakeAuthService(token, getMeFuture: meCompleter.future);
    final notifier = AuthNotifier(service);

    await notifier.init();

    meCompleter.completeError(TimeoutException('offline'));
    await Future<void>.delayed(const Duration(milliseconds: 10));

    expect(notifier.user, isNotNull,
        reason: 'a network hiccup must not sign the user out mid-session');
    expect(service.loggedOut, isFalse);
  });

  test('a blocking 401 records login_required, not resumed', () async {
    final token =
        _fakeJwt(DateTime.now().toUtc().subtract(const Duration(minutes: 1)));
    final service = _FakeAuthService(token,
        getMeError: ApiException(401, '{"detail":"expired"}'));
    final notifier = AuthNotifier(service);

    await notifier.init();

    expect(notifier.user, isNull);
    expect(service.appOpenedCalls, ['login_required']);
  });

  test('no persisted session at all records login_required', () async {
    final service = _FakeNoSessionAuthService();
    final notifier = AuthNotifier(service);

    await notifier.init();

    expect(notifier.user, isNull);
    expect(service.appOpenedCalls, ['login_required']);
  });
}
