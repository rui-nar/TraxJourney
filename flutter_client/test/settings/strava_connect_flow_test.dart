// Strava connect bound to the client that started it
// (docs/STRAVA_CONNECT_BINDING_PLAN.md, U2): the verifier/challenge pair, the
// pending connect's storage and expiry, and how server answers map to
// outcomes.

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
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
  final starts = <({String challenge, String returnTo})>[];
  final completes = <({String code, String state, String verifier})>[];
  ApiException? startError;
  ApiException? completeError;

  @override
  Future<String> startStravaConnect(String challenge, String returnTo) async {
    starts.add((challenge: challenge, returnTo: returnTo));
    if (startError != null) throw startError!;
    return 'https://www.strava.com/oauth/authorize?state=s';
  }

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

/// A popup that records what the flow does to it; [result] is under the
/// test's control.
class _RecordingHandle implements StravaPopupHandle {
  final calls = <String>[];
  final completer = Completer<StravaOAuthResult?>();
  @override
  void navigate(String url) => calls.add('navigate');
  @override
  Future<StravaOAuthResult?> get result => completer.future;
  @override
  void close() => calls.add('close');
}

void main() {
  late _FakeService service;
  late _MemKv kv;
  late DateTime now;
  late StravaConnectFlow flow;

  setUp(() {
    service = _FakeService();
    kv = _MemKv();
    now = DateTime(2026, 10, 8, 12);
    flow = StravaConnectFlow(
        api: service, store: kv, now: () => now, random: Random(42));
  });

  /// The verifier stored for an app connect.
  String storedVerifier() =>
      (jsonDecode(kv.data.values.single) as Map)['verifier'] as String;

  test('challenge matches the RFC 7636 Appendix B vector', () async {
    expect(
      await StravaConnectFlow.challengeFor(
          'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),
      'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
    );
  });

  test('start posts a 43-char challenge of the verifier and return_to web',
      () async {
    final url = await flow.start(app: false);
    expect(url.host, 'www.strava.com');
    expect(service.starts.single.returnTo, 'web');
    expect(service.starts.single.challenge, hasLength(43));
    expect(service.starts.single.challenge,
        matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
    // Web keeps the verifier in memory only.
    expect(kv.data, isEmpty);

    await flow.complete('c', 's');
    final verifier = service.completes.single.verifier;
    expect(verifier, hasLength(43));
    expect(await StravaConnectFlow.challengeFor(verifier),
        service.starts.single.challenge);
  });

  test('start for the app posts return_to app and stores the verifier '
      'with its expiry', () async {
    await flow.start(app: true);
    expect(service.starts.single.returnTo, 'app');
    final stored = jsonDecode(kv.data.values.single) as Map;
    expect(stored['expires_at'],
        now.add(const Duration(minutes: 10)).millisecondsSinceEpoch);
    expect(await StravaConnectFlow.challengeFor(stored['verifier'] as String),
        service.starts.single.challenge);
  });

  test('complete sends the stored verifier and clears it', () async {
    await flow.start(app: true);
    final verifier = storedVerifier();

    // A fresh flow, as after Android killed the app while at Strava.
    final restarted = StravaConnectFlow(api: service, store: kv, now: () => now);
    expect(await restarted.complete('the-code', 'the-state'),
        StravaConnectOutcome.connected);
    expect(service.completes.single,
        (code: 'the-code', state: 'the-state', verifier: verifier));
    expect(kv.data, isEmpty);

    // Cleared: a second completion has nothing to send.
    expect(await restarted.complete('the-code', 'the-state'),
        StravaConnectOutcome.noPendingConnect);
    expect(service.completes, hasLength(1));
  });

  test('an expired pending connect yields noPendingConnect without an API call',
      () async {
    await flow.start(app: true);
    now = now.add(const Duration(minutes: 10, seconds: 1));
    expect(await flow.complete('c', 's'), StravaConnectOutcome.noPendingConnect);
    expect(service.completes, isEmpty);
    expect(kv.data, isEmpty);
  });

  test('no pending connect yields noPendingConnect without an API call',
      () async {
    expect(await flow.complete('c', 's'), StravaConnectOutcome.noPendingConnect);
    expect(service.completes, isEmpty);
  });

  test('403 maps to wrongAccount and clears the pending connect', () async {
    service.completeError = ApiException(403, '{"detail":"Forbidden"}');
    await flow.start(app: true);
    expect(await flow.complete('c', 's'), StravaConnectOutcome.wrongAccount);
    expect(kv.data, isEmpty);
  });

  test('400 state_expired maps to expired', () async {
    service.completeError = ApiException(400, '{"detail":"state_expired"}');
    await flow.start(app: false);
    expect(await flow.complete('c', 's'), StravaConnectOutcome.expired);
  });

  test('400 invalid_state and 502 map to failed', () async {
    service.completeError = ApiException(400, '{"detail":"invalid_state"}');
    await flow.start(app: false);
    expect(await flow.complete('c', 's'), StravaConnectOutcome.failed);

    service.completeError = ApiException(502, '{"detail":"Strava error"}');
    await flow.start(app: false);
    expect(await flow.complete('c', 's'), StravaConnectOutcome.failed);
  });

  test('426 on start throws updateRequired', () async {
    service.startError = ApiException(426,
        '{"detail":"Update the app to connect Strava."}');
    await expectLater(
      flow.start(app: true),
      throwsA(isA<StravaConnectException>().having(
          (e) => e.outcome, 'outcome', StravaConnectOutcome.updateRequired)),
    );
  });

  test('other start failures are rethrown as they came', () async {
    service.startError = ApiException(503, '{"detail":"not configured"}');
    await expectLater(flow.start(app: false), throwsA(isA<ApiException>()));
  });

  test('a new start replaces the previous verifier', () async {
    await flow.start(app: true);
    final first = storedVerifier();
    await flow.start(app: true);
    final second = storedVerifier();
    expect(second, isNot(first));
    expect(await StravaConnectFlow.challengeFor(second),
        service.starts.last.challenge);

    await flow.complete('c', 's');
    expect(service.completes.single.verifier, second);
  });

  test('a new web start replaces the previous in-memory verifier', () async {
    await flow.start(app: false);
    await flow.start(app: false);
    await flow.complete('c', 's');
    expect(await StravaConnectFlow.challengeFor(service.completes.single.verifier),
        service.starts.last.challenge);
  });

  group('connectWeb popup handling', () {
    late _RecordingHandle handle;
    late List<StravaConnectOutcome> published;
    late StravaConnectFlow webFlow;
    var opened = 0;
    StravaPopupHandle? opening;

    setUp(() {
      handle = _RecordingHandle();
      opened = 0;
      opening = handle;
      published = [];
      webFlow = StravaConnectFlow(
        api: service,
        store: kv,
        now: () => now,
        random: Random(42),
        openPopup: () {
          opened++;
          return opening;
        },
      );
      webFlow.outcomes.listen(published.add);
    });

    test('popup is opened before start is awaited', () async {
      final done = webFlow.connectWeb();
      // Synchronously, before any await: opened, but start not yet reached.
      expect(opened, 1);
      expect(service.starts, isEmpty);
      await pumpEventQueue();
      expect(service.starts, hasLength(1));
      expect(handle.calls, ['navigate']);
      handle.completer.complete(null);
      await done;
    });

    test('blocked popup publishes popupBlocked and never calls start',
        () async {
      opening = null;
      await webFlow.connectWeb();
      await pumpEventQueue();
      expect(service.starts, isEmpty);
      expect(published, [StravaConnectOutcome.popupBlocked]);
      expect(kv.data, isEmpty);
    });

    test('closed popup publishes cancelled and clears the pending connect',
        () async {
      final done = webFlow.connectWeb();
      await pumpEventQueue();
      handle.completer.complete(null);
      await done;
      await pumpEventQueue();
      expect(published, [StravaConnectOutcome.cancelled]);
      expect(service.completes, isEmpty);

      // The pending connect is gone: a late relay finds nothing to finish.
      expect(await webFlow.complete('c', 's'),
          StravaConnectOutcome.noPendingConnect);
    });

    test('start failure closes the opened popup and rethrows', () async {
      service.startError = ApiException(500, 'boom');
      await expectLater(webFlow.connectWeb(), throwsA(isA<ApiException>()));
      expect(handle.calls, ['close']);
      expect(published, isEmpty);
    });

    test('a 426 from start closes the popup and throws updateRequired',
        () async {
      service.startError = ApiException(426, '');
      await expectLater(
          webFlow.connectWeb(),
          throwsA(isA<StravaConnectException>().having(
              (e) => e.outcome, 'outcome', StravaConnectOutcome.updateRequired)));
      expect(handle.calls, ['close']);
    });
  });

  test('cancelled and popupBlocked have their own messages', () {
    expect(stravaConnectMessage(StravaConnectOutcome.cancelled),
        'Strava connection cancelled.');
    expect(stravaConnectMessage(StravaConnectOutcome.popupBlocked),
        'Allow pop-ups for TraxJourney to connect Strava.');
  });

  test('relayed reason tokens map to outcomes', () {
    expect(stravaOutcomeForReason('denied'), StravaConnectOutcome.denied);
    expect(stravaOutcomeForReason('state_expired'), StravaConnectOutcome.expired);
    expect(stravaOutcomeForReason('update_required'),
        StravaConnectOutcome.updateRequired);
    expect(stravaOutcomeForReason('invalid_state'), StravaConnectOutcome.failed);
    expect(stravaOutcomeForReason(null), StravaConnectOutcome.failed);
  });
}
