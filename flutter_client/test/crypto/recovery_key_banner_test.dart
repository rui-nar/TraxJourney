import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/crypto/recovery_key_banner.dart';
import 'package:traxjourney_client/src/crypto/replace_recovery_key_screen.dart'
    show kReplaceRecoveryKeyRoute;

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

/// One account, one trusted device; [unconfirmed] is what the status lists.
class _Server implements EncryptionApi {
  String? devicePub, deviceWrap, deviceEph;
  List<String> unconfirmed = const ['recovery_key'];

  @override
  Future<String?> enable(Map<String, dynamic> payload) async {
    final d = payload['device'] as Map<String, dynamic>;
    devicePub = d['public_key'] as String;
    deviceWrap = d['wrapped_cmk'] as String;
    deviceEph = d['ephemeral_public_key'] as String;
    return null;
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? pub) async => EncryptionStatus(
        enabled: devicePub != null,
        recoveryMethods: const ['recovery_key'],
        deviceRegistered: pub == devicePub,
        deviceApproved: pub == devicePub,
        wrappedCmkB64: pub == devicePub ? deviceWrap : null,
        ephemeralPublicKeyB64: pub == devicePub ? deviceEph : null,
        unconfirmedRecoveryMethods: unconfirmed,
      );

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async => null;
  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {}
  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async =>
      wrappedCmkB64;
  @override
  Future<void> registerDevice(String publicKeyB64, String label) async {}
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String a, String b, String c) async {}
}

/// A service on the device that turned encryption on, signed out: the next
/// [EncryptionService.prepareForSession] unlocks it.
Future<EncryptionService> _trustedDevice(_Server server) async {
  final store = _FakeStore();
  await EncryptionService(store, server).enable(const RecoveryKeyChoice());
  return EncryptionService(store, server);
}

const _prompt = 'Your recovery key was never confirmed. Create a new one now.';

Future<void> _pump(WidgetTester tester, EncryptionService svc) =>
    tester.pumpWidget(MaterialApp.router(
      routerConfig: GoRouter(routes: [
        GoRoute(
          path: '/',
          builder: (_, __) =>
              Scaffold(body: RecoveryKeyBanner(service: svc)),
        ),
        GoRoute(
          path: kReplaceRecoveryKeyRoute,
          builder: (_, __) => const Text('replace screen'),
        ),
      ]),
    ));

void main() {
  testWidgets('shows once unlocked with an unconfirmed recovery key, '
      'and not after lock()', (tester) async {
    final svc = await _trustedDevice(_Server());
    await _pump(tester, svc);
    expect(find.text(_prompt), findsNothing, reason: 'still locked');

    // The unlock lands after the banner is built, as on a real sign-in.
    expect(await svc.prepareForSession(), isTrue);
    await tester.pump();
    expect(find.text(_prompt), findsOneWidget);

    svc.lock();
    // The lock is heard on a microtask, which schedules the next frame.
    await tester.pumpAndSettle();
    expect(find.text(_prompt), findsNothing);
  });

  testWidgets('hidden when the recovery key is confirmed', (tester) async {
    final server = _Server()..unconfirmed = const [];
    final svc = await _trustedDevice(server);
    expect(await svc.prepareForSession(), isTrue);
    await _pump(tester, svc);
    await tester.pump();
    expect(find.text(_prompt), findsNothing);
  });

  testWidgets('hidden while locked, whatever the server lists',
      (tester) async {
    final server = _Server();
    final svc = await _trustedDevice(server);
    await svc.prepareForSession();
    svc.lock();
    await _pump(tester, svc);
    await tester.pump();
    expect(find.text(_prompt), findsNothing);
  });

  testWidgets('Create opens the replacement screen', (tester) async {
    final svc = await _trustedDevice(_Server());
    expect(await svc.prepareForSession(), isTrue);
    await _pump(tester, svc);
    await tester.pump();

    await tester.tap(find.text('Create'));
    await tester.pumpAndSettle();
    expect(find.text('replace screen'), findsOneWidget);
  });
}
