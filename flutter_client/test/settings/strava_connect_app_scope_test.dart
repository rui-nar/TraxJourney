// A web Strava connect is app-scoped (docs/STRAVA_CONNECT_BINDING_PLAN.md,
// U2, review finding U2-1): leaving the screen that started it must not lose
// it, and its outcome reaches the app-level messenger once.

import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/device_key_store.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/strava_connect_flow.dart';
import 'package:traxjourney_client/src/settings/strava_oauth_popup_stub.dart';

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
  final challenges = <String>[];
  final completes = <({String code, String state, String verifier})>[];

  @override
  Future<String> startStravaConnect(String challenge, String returnTo) async {
    challenges.add(challenge);
    return 'https://www.strava.com/oauth/authorize';
  }

  @override
  Future<void> completeStravaConnect({
    required String code,
    required String state,
    required String verifier,
  }) async =>
      completes.add((code: code, state: state, verifier: verifier));
}

/// A popup that relays its result only when the test says so.
class _FakePopup {
  final pending = <Completer<StravaOAuthResult>>[];
  Future<StravaOAuthResult> open(String url) {
    final c = Completer<StravaOAuthResult>();
    pending.add(c);
    return c.future;
  }
}

/// Stands for the Settings screen: starts a web connect, then goes away.
class _Starter extends StatelessWidget {
  final StravaConnectFlow flow;
  const _Starter(this.flow);
  @override
  Widget build(BuildContext context) => Scaffold(
        body: TextButton(
          onPressed: () => flow.connectWeb(),
          child: const Text('Connect'),
        ),
      );
}

void main() {
  late _FakeService service;
  late _FakePopup popup;
  late StravaConnectFlow flow;

  setUp(() {
    service = _FakeService();
    popup = _FakePopup();
    flow = StravaConnectFlow(
      api: service,
      store: _MemKv(),
      random: Random(7),
      openPopup: popup.open,
    );
  });

  testWidgets(
      'a web connect completes after the screen that started it is gone, '
      'and the app-level messenger shows the outcome once', (tester) async {
    final messenger = GlobalKey<ScaffoldMessengerState>();
    final shown = showStravaConnectOutcomes(messenger, flow);
    addTearDown(shown.cancel);
    final published = <StravaConnectOutcome>[];
    final sub = flow.outcomes.listen(published.add);
    addTearDown(sub.cancel);

    await tester.pumpWidget(MaterialApp(
        scaffoldMessengerKey: messenger, home: _Starter(flow)));
    await tester.tap(find.text('Connect'));
    await tester.pump();
    expect(popup.pending, hasLength(1));

    // The user leaves: the starter is disposed while the popup is open.
    await tester.pumpWidget(MaterialApp(
        scaffoldMessengerKey: messenger,
        home: const Scaffold(body: Text('Elsewhere'))));
    expect(find.byType(_Starter), findsNothing);

    popup.pending.single.complete((code: 'the-code', state: 'the-state', error: null));
    await tester.pump();
    await tester.pump();

    expect(service.completes, hasLength(1));
    expect(service.completes.single.code, 'the-code');
    expect(await StravaConnectFlow.challengeFor(service.completes.single.verifier),
        service.challenges.single);
    expect(published, [StravaConnectOutcome.connected]);
    expect(find.text('Strava connected!'), findsOneWidget);
  });

  test('a relayed error publishes its outcome once without completing',
      () async {
    final published = <StravaConnectOutcome>[];
    final sub = flow.outcomes.listen(published.add);
    addTearDown(sub.cancel);

    final done = flow.connectWeb();
    await pumpEventQueue();
    popup.pending.single.complete((code: null, state: null, error: 'denied'));
    await done;
    await pumpEventQueue();

    expect(service.completes, isEmpty);
    expect(published, [StravaConnectOutcome.denied]);
  });

  test('a second start replaces the first: only its code is completed, '
      'with its verifier', () async {
    final published = <StravaConnectOutcome>[];
    final sub = flow.outcomes.listen(published.add);
    addTearDown(sub.cancel);

    final first = flow.connectWeb();
    await pumpEventQueue();
    final second = flow.connectWeb();
    await pumpEventQueue();
    expect(popup.pending, hasLength(2));

    popup.pending[0].complete((code: 'code-1', state: 'state-1', error: null));
    await first;
    expect(service.completes, isEmpty);

    popup.pending[1].complete((code: 'code-2', state: 'state-2', error: null));
    await second;
    await pumpEventQueue();

    expect(service.completes, hasLength(1));
    expect(service.completes.single.code, 'code-2');
    expect(await StravaConnectFlow.challengeFor(service.completes.single.verifier),
        service.challenges[1]);
    expect(published, [StravaConnectOutcome.connected]);
  });
}
