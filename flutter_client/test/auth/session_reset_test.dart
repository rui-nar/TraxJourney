// The end of one account's session is the end of its client state (issue
// #418, Decisions 1, 3, 4 and 5 of docs/CLIENT_STATE_MAP_PLAN.md).
//
// Two root causes, both covered here:
//  - a restored session's user id was '' until a fresh login, because
//    /api/auth/me names the account `sub`, not `id`. Everything keyed on it —
//    the last-opened trip, the on-device cache — was one key that every
//    account on the device shared;
//  - the 401s that force a logout only nulled the user: the E2EE key stayed
//    unlocked and fetches started for the old account could be handed to the
//    next.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/last_opened_project.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/project_cache_store_native.dart'
    show keyPrefixRange;
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// An unsigned JWT for account [sub], valid for [life] from now.
String _jwt(int sub, {Duration life = const Duration(days: 3)}) {
  String seg(Object payload) =>
      base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
  final exp = DateTime.now().toUtc().add(life).millisecondsSinceEpoch ~/ 1000;
  return '${seg({'alg': 'none'})}.${seg({'sub': '$sub', 'exp': exp})}.sig';
}

/// What /api/auth/me answers: the token's payload, which names the account
/// `sub` and carries no `id`.
Map<String, dynamic> _me(int sub) =>
    {'sub': '$sub', 'email': 'u$sub@example.com', 'auth_provider': 'local'};

/// Restores [token] from "storage"; getMe() answers with [me], fails with
/// [meError], or — when neither is given — never answers.
class _RestoringService extends AuthService {
  _RestoringService(this.token, {this.me, this.meError});

  final String token;
  final Future<Map<String, dynamic>>? me;
  final Object? meError;

  @override
  Future<bool> restoreSession() async {
    api.setToken(token);
    return true;
  }

  @override
  Future<void> appOpened(String sessionState) async {}

  @override
  Future<Map<String, dynamic>> getMe() async {
    if (meError != null) throw meError!;
    return me ?? Completer<Map<String, dynamic>>().future;
  }

  @override
  Future<void> logout() async => api.clearToken();
}

Future<AuthNotifier> _restored(int sub,
    {Future<Map<String, dynamic>>? me,
    Object? meError,
    Duration life = const Duration(days: 3)}) async {
  final auth =
      AuthNotifier(_RestoringService(_jwt(sub, life: life), me: me, meError: meError));
  await auth.init();
  return auth;
}

/// Answers every request but /api/projects/Japan's details, which it holds
/// open and counts.
class _Server {
  int detailsRequests = 0;
  final held = Completer<http.Response>();

  http.Client get client => MockClient((req) async {
        if (req.url.path == '/api/projects/Japan') {
          detailsRequests++;
          return held.future;
        }
        return http.Response('{}', 200);
      });
}

void main() {
  setUpAll(() {
    // The encryption singleton keeps the ApiClient it is first built with.
    // Build it now, on one that answers its calls, before any test can.
    api = ApiClient(
        httpClient: MockClient((_) async => http.Response('{}', 200)));
    expect(encryption.isUnlocked, isFalse);
  });

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    projectDataCache.resetForTest();
    api = ApiClient();
  });

  group('a restored session has its real id', () {
    test('before /api/auth/me answers', () async {
      final auth = await _restored(7);
      expect(auth.user?.id, '7');
    });

    test('offline, when /api/auth/me cannot answer', () async {
      // Near expiry, so init() blocks on getMe(), which fails offline.
      final auth = await _restored(7,
          life: const Duration(minutes: 1),
          meError: TimeoutException('offline'));
      expect(auth.user?.id, '7');
    });

    test('after /api/auth/me answers with the token payload', () async {
      final me = Completer<Map<String, dynamic>>();
      final auth = await _restored(7, me: me.future);
      me.complete(_me(7));
      await pumpEventQueue();
      expect(auth.user?.email, 'u7@example.com', reason: '/me has landed');
      expect(auth.user?.id, '7');
    });

    test('when init() blocks on /api/auth/me', () async {
      final auth = await _restored(7,
          life: const Duration(minutes: 1), me: Future.value(_me(7)));
      expect(auth.user?.id, '7');
    });

    test("and another account's restore does not read its cached trips",
        () async {
      // The cache is keyed "<user>:<owner>:<name>", and every restored session
      // used to be user 0 — so the next account's restore read the last one's.
      const ref = ProjectRef(name: 'Japan');
      await _restored(7);
      projectDataCache.onMetaFetched(ref, {'lock_version': 1, 'name': 'Japan'});
      expect(await projectDataCache.readMetaForOfflineFallback(ref), isNotNull);

      await _restored(8);
      expect(await projectDataCache.readMetaForOfflineFallback(ref), isNull);
    });
  });

  group('a 401 that forces a logout', () {
    Future<void> unlock() async {
      await encryption.enable(const RecoveryKeyChoice());
      expect(encryption.isUnlocked, isTrue);
    }

    // A closure, not a tear-off: a tear-off would build the singleton while
    // the tests are declared, before setUpAll hands it its client.
    tearDown(() => encryption.lock());

    test('at startup locks E2EE', () async {
      await unlock();
      final auth = await _restored(7,
          life: const Duration(minutes: 1),
          meError: ApiException(401, '{"detail":"expired"}'));
      expect(auth.user, isNull);
      expect(encryption.isUnlocked, isFalse);
    });

    test('in the background locks E2EE', () async {
      await unlock();
      final me = Completer<Map<String, dynamic>>();
      final auth = await _restored(7, me: me.future);
      expect(auth.user, isNotNull);

      me.completeError(ApiException(401, '{"detail":"revoked"}'));
      await pumpEventQueue();

      expect(auth.user, isNull);
      expect(encryption.isUnlocked, isFalse);
    });

    test('forgets the fetches the account had in flight', () async {
      final server = _Server();
      final me = Completer<Map<String, dynamic>>();
      final auth = await _restored(7, me: me.future);
      api = ApiClient(httpClient: server.client)..setToken(_jwt(7));

      final first = ProjectService().getDetails(const ProjectRef(name: 'Japan'));
      first.ignore();
      await pumpEventQueue();
      expect(server.detailsRequests, 1);

      me.completeError(ApiException(401, '{"detail":"revoked"}'));
      await pumpEventQueue();
      expect(auth.user, isNull);

      // The next account opens a trip of the same name: it gets a request of
      // its own, not the previous account's response.
      api.setToken(_jwt(8));
      ProjectService().getDetails(const ProjectRef(name: 'Japan')).ignore();
      await pumpEventQueue();
      expect(server.detailsRequests, 2);
    });
  });

  group('ProjectNotifier follows the signed-in account', () {
    late ProjectNotifier notifier;
    setUp(() => notifier = ProjectNotifier(ProjectService()));

    void hold() {
      notifier
        ..ref = const ProjectRef(name: 'Japan')
        ..people = [
          {'id': 1}
        ];
    }

    test('the first account it sees is not a change', () {
      hold();
      notifier.onAuthChanged('7');
      expect(notifier.ref, isNotNull);
    });

    test('a logout clears it', () {
      notifier.onAuthChanged('7');
      hold();
      notifier.onAuthChanged(null);
      expect(notifier.ref, isNull);
      expect(notifier.people, isEmpty);
    });

    test('another account clears it', () {
      notifier.onAuthChanged('7');
      hold();
      notifier.onAuthChanged('8');
      expect(notifier.ref, isNull);
    });

    test('the same account, refreshed, does not', () {
      // /api/auth/me landing after a restore, a profile refresh, a verified
      // email: same id, same session, and a trip being opened stays open.
      notifier.onAuthChanged('7');
      hold();
      notifier.onAuthChanged('7');
      expect(notifier.ref, isNotNull);
    });

    test('the end of a session restore does not', () {
      // A trip deep-linked under the splash loads with the restored token;
      // the restore then names its account, null to '7'.
      notifier.onAuthChanged(null, restoring: true);
      hold();
      notifier.onAuthChanged('7');
      expect(notifier.ref, isNotNull);
    });

    test('signing out and back in as the same account clears it', () {
      notifier.onAuthChanged('7');
      hold();
      notifier.onAuthChanged(null);
      hold();
      notifier.onAuthChanged('7');
      expect(notifier.ref, isNull);
    });
  });

  group('last_opened_project', () {
    test('two accounts on one device keep their own', () async {
      final a = await _restored(7);
      await saveLastOpenedProject(a.user?.id, const ProjectRef(name: 'Japan'));
      final b = await _restored(8);
      await saveLastOpenedProject(b.user?.id, const ProjectRef(name: 'Peru'));

      expect((await readLastOpenedProject(a.user?.id))?.name, 'Japan');
      expect((await readLastOpenedProject(b.user?.id))?.name, 'Peru');
      expect(await rootRedirectTarget(b.user?.id), '/view?project=Peru');
    });

    test('nothing is read or written for an unknown id', () async {
      await saveLastOpenedProject('', const ProjectRef(name: 'Japan'));
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), isEmpty);
      SharedPreferences.setMockInitialValues(
          {'last_opened_project_': '{"name":"Japan"}'});
      expect(await readLastOpenedProject(''), isNull);
    });

    test('the key every restored session shared is purged at startup',
        () async {
      SharedPreferences.setMockInitialValues({
        'last_opened_project_': '{"name":"Japan"}',
        'last_opened_project_7': '{"name":"Peru"}',
      });
      await purgeSharedLastOpenedProject();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.containsKey('last_opened_project_'), isFalse);
      expect(prefs.getString('last_opened_project_7'), '{"name":"Peru"}');
    });
  });

  group("the on-device cache's user-0 entries", () {
    // Keys are "<user>:<owner>:<name>", and user 0 was every restored session.
    const ref = ProjectRef(name: 'Japan');
    const meta = {'lock_version': 1, 'name': 'Japan'};

    test('are purged at startup', () async {
      projectDataCache.setCurrentUser(null); // keyed to user 0
      projectDataCache.onMetaFetched(ref, meta);
      expect(await projectDataCache.readMetaForOfflineFallback(ref), isNotNull);

      await projectDataCache.purgeUserZero();

      expect(await projectDataCache.readMetaForOfflineFallback(ref), isNull);
    });

    test("and another account's survive", () async {
      projectDataCache.setCurrentUser(7);
      projectDataCache.onMetaFetched(ref, meta);

      await projectDataCache.purgeUserZero();

      expect(await projectDataCache.readMetaForOfflineFallback(ref), isNotNull);
    });

    test('on disk: the deleted key range holds user 0 and nothing else', () {
      // No sqflite backend runs under flutter test, so this checks the range
      // the store deletes, compared the way SQLite compares TEXT by default:
      // byte by byte, as UTF-8.
      final (lower, upper) = keyPrefixRange('0:');
      bool deleted(String key) =>
          _compareBinary(key, lower) >= 0 && _compareBinary(key, upper) < 0;

      for (final key in ['0:0:Japan', '0:7:Japan', '0:0:', '0:0:日本']) {
        expect(deleted(key), isTrue, reason: key);
      }
      for (final key in [
        '7:0:Japan', '7:0:0:Japan', '10:0:Japan', '01:0:Japan', '0;0:Japan',
        '0', '00:0:Japan', '7:0:日本',
      ]) {
        expect(deleted(key), isFalse, reason: key);
      }
    });

    test('a prefix is never a pattern', () {
      // LIKE would read these as wildcards; the range reads them literally.
      expect(keyPrefixRange('0_'), ('0_', '0`'));
      expect(keyPrefixRange('0%'), ('0%', '0&'));
      expect(() => keyPrefixRange(''), throwsArgumentError);
      expect(() => keyPrefixRange('日'), throwsArgumentError);
    });
  });
}

/// SQLite's default BINARY collation: memcmp of the UTF-8 bytes.
int _compareBinary(String a, String b) {
  final x = utf8.encode(a), y = utf8.encode(b);
  for (var i = 0; i < x.length && i < y.length; i++) {
    if (x[i] != y[i]) return x[i] - y[i];
  }
  return x.length - y.length;
}
