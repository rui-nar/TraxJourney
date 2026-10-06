import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/crypto/replace_recovery_key_screen.dart';

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

/// One account on a Decision 16 server: one trusted device, one recovery key
/// that starts unconfirmed.
class _Server implements EncryptionApi {
  String? devicePub, deviceWrap, deviceEph;
  RecoveryWrapData? recovery;
  bool unconfirmed = false;
  final replaces = <String>[];
  final confirms = <String>[];

  @override
  Future<String?> enable(Map<String, dynamic> payload) async {
    final d = payload['device'] as Map<String, dynamic>;
    devicePub = d['public_key'] as String;
    deviceWrap = d['wrapped_cmk'] as String;
    deviceEph = d['ephemeral_public_key'] as String;
    final r = payload['recovery'] as Map<String, dynamic>;
    recovery = RecoveryWrapData(r['wrapped_cmk'] as String, r['salt'] as String, null);
    unconfirmed = true;
    return recovery!.wrappedCmkB64;
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? pub) async => EncryptionStatus(
        enabled: devicePub != null,
        recoveryMethods: const ['recovery_key'],
        deviceRegistered: pub == devicePub,
        deviceApproved: pub == devicePub,
        wrappedCmkB64: pub == devicePub ? deviceWrap : null,
        ephemeralPublicKeyB64: pub == devicePub ? deviceEph : null,
        unconfirmedRecoveryMethods: [if (unconfirmed) 'recovery_key'],
      );

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async => recovery;

  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {
    confirms.add(wrappedCmkB64);
    if (wrappedCmkB64 != recovery!.wrappedCmkB64) throw const RecoveryKeyConflict();
    unconfirmed = false;
  }

  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async {
    replaces.add(wrappedCmkB64);
    if (!unconfirmed) throw const RecoveryKeyConflict();
    recovery = RecoveryWrapData(wrappedCmkB64, saltB64, null);
    return wrappedCmkB64;
  }

  @override
  Future<void> registerDevice(String publicKeyB64, String label) async {}
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String a, String b, String c) async {}
}

/// Signed in again on the device that turned encryption on, never having
/// confirmed the recovery key.
Future<EncryptionService> _signedIn(_Server server) async {
  final store = _FakeStore();
  await EncryptionService(store, server).enable(const RecoveryKeyChoice());
  final svc = EncryptionService(store, server);
  expect(await svc.prepareForSession(), isTrue);
  return svc;
}

Future<void> _pump(WidgetTester tester, EncryptionService svc) async {
  tester.view.physicalSize = const Size(1080, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
      MaterialApp(home: ReplaceRecoveryKeyScreen(service: svc)));
}

void main() {
  testWidgets('sends one replacement, shows the new key and confirms it',
      (tester) async {
    final server = _Server();
    final svc = await _signedIn(server);
    final oldWrap = server.recovery!.wrappedCmkB64;
    await _pump(tester, svc);

    await tester.tap(find.text('Create a new recovery key'));
    await tester.pumpAndSettle();

    expect(server.replaces, hasLength(1));
    expect(server.recovery!.wrappedCmkB64, isNot(oldWrap));
    expect(find.text('Save your recovery key'), findsOneWidget);

    // The key on screen is the one the server now holds.
    final shown = tester.widget<SelectableText>(find.byType(SelectableText)).data!;
    final hex = shown.replaceAll('-', '');
    final secret = [
      for (var i = 0; i < hex.length; i += 2)
        int.parse(hex.substring(i, i + 2), radix: 16)
    ];
    expect(
        await EncryptionService(_FakeStore(), server)
            .recoverWithRecoveryKey(secret),
        isTrue);

    await tester.tap(find.text("I've saved my recovery key somewhere safe"));
    await tester.pump();
    await tester.tap(find.text('Done'));
    await tester.pumpAndSettle();

    expect(server.confirms, [server.replaces.single]);
    expect(server.unconfirmed, isFalse);
    expect(svc.needsRecoveryKeyReplacement, isFalse);
    expect(find.text('Recovery key saved'), findsOneWidget);
  });

  testWidgets('a key already confirmed is not replaced', (tester) async {
    final server = _Server();
    final svc = await _signedIn(server);
    server.unconfirmed = false; // confirmed on another device meanwhile
    await _pump(tester, svc);

    await tester.tap(find.text('Create a new recovery key'));
    await tester.pumpAndSettle();

    expect(find.textContaining('already been confirmed'), findsOneWidget);
    expect(find.byType(SelectableText), findsNothing);
    expect(svc.needsRecoveryKeyReplacement, isFalse,
        reason: 'the status was refetched');
  });

  testWidgets('after the session ended, nothing is sent', (tester) async {
    final server = _Server();
    final svc = await _signedIn(server);
    svc.lock();
    await _pump(tester, svc);

    await tester.tap(find.text('Create a new recovery key'));
    await tester.pumpAndSettle();

    expect(server.replaces, isEmpty);
    expect(find.byType(SelectableText), findsNothing);
  });
}
