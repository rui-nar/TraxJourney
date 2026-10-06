import 'dart:async';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/crypto/enable_encryption_screen.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

class _FakeApi implements EncryptionApi {
  @override
  Future<void> enable(Map<String, dynamic> payload) async {}
  @override
  Future<EncryptionStatus> fetchStatus(String? d) async => const EncryptionStatus(
        enabled: false, recoveryMethods: [],
        deviceRegistered: false, deviceApproved: false,
      );
  @override
  Future<void> registerDevice(String publicKeyB64, String label) async {}
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String a, String b, String c) async {}
  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async => null;
  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {}
  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async =>
      wrappedCmkB64;
}

/// Holds the enable request until [gate] completes.
class _HeldApi extends _FakeApi {
  final gate = Completer<void>();
  @override
  Future<void> enable(Map<String, dynamic> payload) => gate.future;
}

Widget _wrap() => MaterialApp(
      home: EnableEncryptionScreen(
        service: EncryptionService(_FakeStore(), _FakeApi()),
        onEnabled: (_) async {}, // skip the real migration (no network in tests)
      ),
    );

/// Pump on a tall surface so the whole scrolling form is laid out (the lazy
/// ListView otherwise won't build widgets below the default 600px test height).
Future<void> _pump(WidgetTester tester) async {
  tester.view.physicalSize = const Size(1080, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(_wrap());
}

/// Answers enable with [stored] (null: an older server that does not say) and
/// records every confirm; throws [confirmError] from confirm when set.
class _ConfirmApi extends _FakeApi {
  final String? stored;
  Object? confirmError;
  Map<String, dynamic>? enabled;
  final confirms = <(String, String)>[];
  int statusCalls = 0;

  _ConfirmApi({this.stored = 'STORED-WRAP', this.confirmError});

  @override
  Future<String?> enable(Map<String, dynamic> payload) async {
    enabled = payload;
    return stored;
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? d) {
    statusCalls++;
    return super.fetchStatus(d);
  }

  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {
    confirms.add((method, wrappedCmkB64));
    if (confirmError != null) throw confirmError!;
  }
}

/// Turns encryption on with a recovery key over [api], ticks "I've saved it"
/// and taps Done.
Future<void> _enableAndConfirm(WidgetTester tester, EncryptionApi api) async {
  tester.view.physicalSize = const Size(1080, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(MaterialApp(
    home: EnableEncryptionScreen(
      service: EncryptionService(_FakeStore(), api),
      onEnabled: (_) async {},
    ),
  ));
  await tester.tap(find.text('Recovery key'));
  await tester.pump();
  await tester.tap(find.text('Turn on encryption'));
  await tester.pumpAndSettle();
  await tester.tap(find.text("I've saved my recovery key somewhere safe"));
  await tester.pump();
  await tester.tap(find.text('Done'));
  await tester.pumpAndSettle();
}

const _couldNotRecord =
    "Couldn't record that you saved it; you'll be asked again.";

void main() {
  testWidgets('renders the three security levels', (tester) async {
    await _pump(tester);
    expect(find.text('High  ·  Strongest'), findsOneWidget);
    expect(find.text('Medium  ·  Security questions'), findsOneWidget);
    expect(find.textContaining('Low'), findsOneWidget);
  });

  testWidgets('Low is presented honestly but not selectable yet', (tester) async {
    await _pump(tester);
    // Honest copy: recoverable -> operator could read it.
    expect(find.textContaining('an administrator could read it'), findsOneWidget);
    // Tapping Low does not enable the turn-on button (backend not built).
    await tester.tap(find.textContaining('Low'));
    await tester.pump();
    final btn = tester.widget<FilledButton>(
      find.ancestor(of: find.text('Turn on encryption'), matching: find.byType(FilledButton)),
    );
    expect(btn.onPressed, isNull);
  });

  testWidgets('selecting Medium reveals the weaker-option warning', (tester) async {
    await _pump(tester);
    expect(find.textContaining('access to the server'), findsNothing);

    await tester.tap(find.text('Medium  ·  Security questions'));
    await tester.pump();

    expect(find.textContaining('access to the server'), findsOneWidget);
    expect(find.textContaining('Pick at least 3'), findsOneWidget);
    expect(find.text('Add a question'), findsOneWidget); // dropdown to pick from
  });

  testWidgets('Medium: picking a question from the dropdown adds an answer field',
      (tester) async {
    await _pump(tester);
    await tester.tap(find.text('Medium  ·  Security questions'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Add a question'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('What city were you born in?').last);
    await tester.pumpAndSettle();

    // The chosen question is now shown with a remove button + answer field.
    expect(find.text('What city were you born in?'), findsOneWidget);
    expect(find.byIcon(Icons.close), findsOneWidget);
  });

  testWidgets('High + recovery key reveals a one-time key to save', (tester) async {
    await _pump(tester);
    // High is the default; switch its method to the generated recovery key.
    await tester.tap(find.text('Recovery key'));
    await tester.pump();
    await tester.tap(find.text('Turn on encryption'));
    await tester.pumpAndSettle();

    expect(find.text('Save your recovery key'), findsOneWidget);
    // "Done" stays disabled until the user confirms they saved it.
    final doneBtn = tester.widget<FilledButton>(
      find.ancestor(of: find.text('Done'), matching: find.byType(FilledButton)),
    );
    expect(doneBtn.onPressed, isNull);

    await tester.tap(find.text("I've saved my recovery key somewhere safe"));
    await tester.pump();
    final doneBtn2 = tester.widget<FilledButton>(
      find.ancestor(of: find.text('Done'), matching: find.byType(FilledButton)),
    );
    expect(doneBtn2.onPressed, isNotNull);
  });

  testWidgets('the screen cannot be left while the request is out (U5-R3-1)',
      (tester) async {
    tester.view.physicalSize = const Size(1080, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = _HeldApi();
    final navigator = GlobalKey<NavigatorState>();
    await tester.pumpWidget(MaterialApp(
      navigatorKey: navigator,
      home: const Text('home'),
    ));
    navigator.currentState!.push(MaterialPageRoute<void>(
      builder: (_) => EnableEncryptionScreen(
        service: EncryptionService(_FakeStore(), api),
        onEnabled: (_) async {},
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.byType(BackButton), findsOneWidget);

    await tester.tap(find.text('Recovery key'));
    await tester.pump();
    await tester.tap(find.text('Turn on encryption'));
    // Key generation runs real async work; let it reach the held request.
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 200)));
    await tester.pump();

    expect(find.byType(BackButton), findsNothing, reason: 'no back button');
    await navigator.currentState!.maybePop();
    // The busy spinner never settles; a route transition would be done here.
    await tester.pump(const Duration(seconds: 1));
    expect(find.byType(EnableEncryptionScreen), findsOneWidget,
        reason: 'a system back does not pop it either');

    api.gate.complete();
    await tester.pumpAndSettle();
    expect(find.text('Save your recovery key'), findsOneWidget);
  });

  group('confirming the recovery key (Decision 16)', () {
    testWidgets('Done confirms the wrap the server stored', (tester) async {
      final api = _ConfirmApi();
      await _enableAndConfirm(tester, api);

      expect(api.confirms, [('recovery_key', 'STORED-WRAP')]);
      expect(find.text('Encryption is on'), findsOneWidget);
    });

    testWidgets('against an older server, Done confirms the wrap it sent',
        (tester) async {
      final api = _ConfirmApi(stored: null);
      await _enableAndConfirm(tester, api);

      final sent = (api.enabled!['recovery'] as Map)['wrapped_cmk'];
      expect(api.confirms, [('recovery_key', sent)]);
      expect(find.text('Encryption is on'), findsOneWidget);
    });

    for (final (name, error) in <(String, Object)>[
      ('404', ApiException(404, '{"detail":"Not Found"}')),
      // An older server: GET /recovery/{method} matches the path (U5b-R2-3).
      ('405 (an older server)', ApiException(405, '{"detail":"Method Not Allowed"}')),
      ('network', http.ClientException('Connection refused')),
    ]) {
      testWidgets('a failed confirm ($name) keeps the key shown and does not '
          'call it unusable', (tester) async {
        final api = _ConfirmApi(confirmError: error);
        await _enableAndConfirm(tester, api);

        expect(api.confirms, hasLength(1));
        expect(find.text('Save your recovery key'), findsOneWidget);
        expect(find.byType(SelectableText), findsOneWidget);
        expect(find.text(_couldNotRecord), findsOneWidget);
        expect(find.textContaining('Discard'), findsNothing);
        expect(find.text('Encryption is on'), findsNothing);
      });
    }

    testWidgets('a 409 says to discard the key and refetches the status '
        '(U5b-R2-4)', (tester) async {
      final api = _ConfirmApi(confirmError: const RecoveryKeyConflict());
      await _enableAndConfirm(tester, api);
      expect(api.statusCalls, 1, reason: 'refetched after the 409');

      expect(find.text('This key is no longer your recovery key. Discard it.'),
          findsOneWidget);
      expect(find.byType(SelectableText), findsNothing,
          reason: 'the key is no longer shown');
      expect(find.text(_couldNotRecord), findsNothing,
          reason: 'no second chance is promised');
    });
  });
}
