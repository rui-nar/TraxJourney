import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
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
}
