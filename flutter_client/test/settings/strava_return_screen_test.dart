// The Android return from Strava consent (docs/STRAVA_CONNECT_BINDING_PLAN.md,
// U3): traxjourney://app/strava-return completes the pending connect once,
// after any session restore, and lands on Settings with the outcome shown.
// Outcomes of a completed connect come from the app-wide messenger, wired here
// as main.dart wires it.

import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/crypto/device_key_store.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/strava_connect_flow.dart';
import 'package:traxjourney_client/src/settings/strava_return_screen.dart';

class _MemKv implements SecureKvStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

class _FakeService extends SettingsService {
  final completes = <({String code, String state, String verifier})>[];
  ApiException? completeError;

  @override
  Future<String> startStravaConnect(String challenge, String returnTo) async =>
      'https://www.strava.com/oauth/authorize';

  @override
  Future<void> completeStravaConnect({
    required String code,
    required String state,
    required String verifier,
  }) async {
    completes.add((code: code, state: state, verifier: verifier));
    if (completeError != null) throw completeError!;
  }
}

/// An AuthNotifier whose session restore the test ends by hand.
class _Auth extends AuthNotifier {
  _Auth() : super(AuthService());
  bool restoring = false;
  @override
  bool get isRestoring => restoring;

  void signIn() => updateUser({'id': '1', 'email': 'a@x.com'});

  void finishRestore() {
    restoring = false;
    notifyListeners();
  }
}

const _settingsText = 'Settings page';

void main() {
  late _FakeService service;
  late StravaConnectFlow flow;
  late _Auth auth;

  setUp(() {
    service = _FakeService();
    flow = StravaConnectFlow(
        api: service, store: _MemKv(), random: Random(3));
    stravaConnect = flow;
    auth = _Auth();
  });

  /// Mounts the app at [location] with a real GoRouter holding the return
  /// route as app_router.dart builds it, and the app-wide outcome messenger.
  Future<void> pumpApp(WidgetTester tester, String location) async {
    final messenger = GlobalKey<ScaffoldMessengerState>();
    final shown = showStravaConnectOutcomes(messenger, flow);
    addTearDown(shown.cancel);
    final router = GoRouter(
      initialLocation: location,
      routes: [
        GoRoute(
          path: kStravaReturnRoute,
          builder: (context, state) {
            final q = state.uri.queryParameters;
            return StravaReturnScreen(
              code: q['code'],
              state: q['state'],
              strava: q['strava'],
              reason: q['reason'],
            );
          },
        ),
        GoRoute(
          path: '/settings',
          builder: (context, state) =>
              const Scaffold(body: Text(_settingsText)),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(ChangeNotifierProvider<AuthNotifier>.value(
      value: auth,
      child: MaterialApp.router(
          scaffoldMessengerKey: messenger, routerConfig: router),
    ));
  }

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 5; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
  }

  const codeReturn =
      'traxjourney://app/strava-return?strava=code&code=the-code&state=the-state';

  testWidgets('a code and state complete the connect once and land on Settings',
      (tester) async {
    auth.signIn();
    await flow.start(app: true);
    final published = <StravaConnectOutcome>[];
    final sub = flow.outcomes.listen(published.add);
    addTearDown(sub.cancel);

    await pumpApp(tester, codeReturn);
    await settle(tester);

    expect(service.completes, hasLength(1));
    expect(service.completes.single.code, 'the-code');
    expect(service.completes.single.state, 'the-state');
    expect(published, [StravaConnectOutcome.connected]);
    expect(find.text(_settingsText), findsOneWidget);
    // Shown once, by the app-wide messenger, not again by the screen.
    expect(find.text('Strava connected!'), findsOneWidget);
  });

  testWidgets('a relayed denial shows the denied message without completing',
      (tester) async {
    auth.signIn();
    await flow.start(app: true);
    final published = <StravaConnectOutcome>[];
    final sub = flow.outcomes.listen(published.add);
    addTearDown(sub.cancel);

    await pumpApp(
        tester, 'traxjourney://app/strava-return?strava=error&reason=denied');
    await settle(tester);

    expect(service.completes, isEmpty);
    expect(published, isEmpty);
    expect(find.text(_settingsText), findsOneWidget);
    expect(find.text('Strava access was not granted.'), findsOneWidget);
  });

  testWidgets('a connect started from another account shows its message',
      (tester) async {
    auth.signIn();
    await flow.start(app: true);
    service.completeError = ApiException(403, '{"detail":"code_not_bound"}');

    await pumpApp(tester, codeReturn);
    await settle(tester);

    expect(service.completes, hasLength(1));
    expect(find.text(_settingsText), findsOneWidget);
    expect(
        find.text(stravaConnectMessage(StravaConnectOutcome.wrongAccount)),
        findsOneWidget);
  });

  testWidgets('a return with no pending connect shows its message',
      (tester) async {
    auth.signIn();

    await pumpApp(tester, codeReturn);
    await settle(tester);

    expect(service.completes, isEmpty);
    expect(find.text(_settingsText), findsOneWidget);
    expect(
        find.text(
            stravaConnectMessage(StravaConnectOutcome.noPendingConnect)),
        findsOneWidget);
  });

  testWidgets(
      'built while the session is restoring, it waits for the restore to end, '
      'then completes once', (tester) async {
    await flow.start(app: true);
    auth.restoring = true;

    await pumpApp(tester, codeReturn);
    await settle(tester);
    expect(service.completes, isEmpty);
    expect(find.text(_settingsText), findsNothing);

    auth.signIn(); // the restored session's user
    await settle(tester);
    expect(service.completes, isEmpty, reason: 'still restoring');

    auth.finishRestore();
    await settle(tester);
    expect(service.completes, hasLength(1));
    expect(find.text(_settingsText), findsOneWidget);

    // Later notifications (a profile refresh) do not complete again.
    auth.signIn();
    await settle(tester);
    expect(service.completes, hasLength(1));
  });

  testWidgets('a restore that ends signed out completes nothing',
      (tester) async {
    await flow.start(app: true);
    auth.restoring = true;

    await pumpApp(tester, codeReturn);
    await settle(tester);
    auth.finishRestore();
    await settle(tester);

    expect(service.completes, isEmpty);
  });
}
