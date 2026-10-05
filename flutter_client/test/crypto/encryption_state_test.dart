/// The client knows where its account and device stand with encryption
/// (#506): `EncryptionService.state` follows the session's status, unlock,
/// recovery and lock, the trip screen's banner shows while the key can't be
/// used, and the server's encryption refusals read as plain words.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_locked_banner.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

import 'encryption_service_test.dart' show FakeDeviceKeyStore, FakeEncryptionApi;

/// Answers every status request with [status], or fails when it is null.
class _StatusApi extends FakeEncryptionApi {
  EncryptionStatus? status;
  _StatusApi(this.status);

  @override
  Future<EncryptionStatus> fetchStatus(String? devicePublicKeyB64) async {
    final s = status;
    if (s == null) throw Exception('offline');
    return s;
  }
}

void main() {
  group('state', () {
    test('starts disabled, and enabling unlocks', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      expect(svc.state.value, EncryptionState.disabled);
      expect(svc.writeBlockedMessage, isNull);

      await svc.enable(const RecoveryKeyChoice());
      expect(svc.state.value, EncryptionState.unlocked);
      expect(svc.writeBlockedMessage, isNull);
    });

    test('lock() on an unlocked device leaves it locked, writes blocked', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await svc.enable(const RecoveryKeyChoice());
      final seen = <EncryptionState>[];
      svc.state.addListener(() => seen.add(svc.state.value));

      svc.lock();

      expect(svc.state.value, EncryptionState.locked);
      expect(seen, [EncryptionState.locked]);
      expect(svc.writeBlockedMessage, kEncryptionLockedMessage);
    });

    test('a session on a trusted device is unlocked', () async {
      final api = FakeEncryptionApi();
      final store = FakeDeviceKeyStore();
      await EncryptionService(store, api).enable(const RecoveryKeyChoice());

      final svc = EncryptionService(store, api);
      expect(await svc.prepareForSession(), isTrue);
      expect(svc.state.value, EncryptionState.unlocked);
    });

    test('a session on an account without encryption is disabled', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await svc.prepareForSession();
      expect(svc.state.value, EncryptionState.disabled);
    });

    test('a session on a device not approved yet is awaiting approval', () async {
      final api = FakeEncryptionApi();
      await EncryptionService(FakeDeviceKeyStore(), api)
          .enable(const RecoveryKeyChoice());

      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svc.prepareForSession(), isFalse);
      expect(svc.state.value, EncryptionState.awaitingApproval);
      expect(svc.writeBlockedMessage, kEncryptionAwaitingApprovalMessage);
      expect(await api.pendingDevices(), hasLength(1));
    });

    test('an approved device whose key will not unwrap is locked', () async {
      final store = FakeDeviceKeyStore();
      await store.save(await generateDeviceKeyPair());
      final garbage = base64.encode(List.filled(48, 1));
      final svc = EncryptionService(
          store,
          _StatusApi(EncryptionStatus(
            enabled: true,
            recoveryMethods: const ['recovery_key'],
            deviceRegistered: true,
            deviceApproved: true,
            wrappedCmkB64: garbage,
            ephemeralPublicKeyB64: base64.encode(List.filled(32, 2)),
          )));

      await expectLater(svc.prepareForSession(), throwsA(anything));
      expect(svc.state.value, EncryptionState.locked);
      expect(svc.isUnlocked, isFalse);
    });

    test('a status that cannot be read leaves the next session unknown, '
        'not the previous account\'s lock', () async {
      final api = _StatusApi(null);
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      await svc.enable(const RecoveryKeyChoice());
      svc.lock();
      expect(svc.state.value, EncryptionState.locked);

      await expectLater(svc.prepareForSession(), throwsException);
      expect(svc.state.value, EncryptionState.disabled);
    });

    test('a recovery unlocks', () async {
      final api = FakeEncryptionApi();
      final secret = (await EncryptionService(FakeDeviceKeyStore(), api)
              .enable(const RecoveryKeyChoice()))
          .recoverySecret!;
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      await svc.prepareForSession();
      expect(svc.state.value, EncryptionState.awaitingApproval);

      expect(await svc.recoverWithRecoveryKey(secret), isTrue);
      expect(svc.state.value, EncryptionState.unlocked);
    });
  });

  group('encryptionRefusalMessage', () {
    String body(String code) => jsonEncode({
          'detail': {'code': code, 'project_id': 3}
        });

    test('names both encryption refusals in plain words', () {
      final locked = encryptionRefusalMessage(409, body('encryption_locked'));
      final notShared = encryptionRefusalMessage(409, body('encryption_not_shared'));
      expect(locked, contains("can't encrypt"));
      expect(notShared, contains("isn't encrypted"));
      for (final m in [locked!, notShared!]) {
        expect(m, isNot(contains('encryption_')));
      }
    });

    test('leaves every other response to the caller', () {
      expect(encryptionRefusalMessage(409, body('stale_write')), isNull);
      expect(encryptionRefusalMessage(400, body('encryption_locked')), isNull);
      expect(encryptionRefusalMessage(409, '{"detail":"Conflict"}'), isNull);
      expect(encryptionRefusalMessage(409, 'not json'), isNull);
    });
  });

  group('EncryptionLockedBanner', () {
    Future<void> pump(WidgetTester tester, EncryptionService svc) =>
        tester.pumpWidget(MaterialApp(
          home: Scaffold(body: Column(children: [EncryptionLockedBanner(service: svc)])),
        ));

    testWidgets('shows while locked, with Manage devices and Recover', (tester) async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await tester.runAsync(() => svc.enable(const RecoveryKeyChoice()));
      svc.lock();
      await pump(tester, svc);

      expect(find.text(kEncryptionLockedMessage), findsOneWidget);
      expect(find.text('Manage devices'), findsOneWidget);
      expect(find.text('Recover access'), findsOneWidget);
    });

    testWidgets('shows while awaiting approval', (tester) async {
      final api = FakeEncryptionApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      await tester.runAsync(() async {
        await EncryptionService(FakeDeviceKeyStore(), api)
            .enable(const RecoveryKeyChoice());
        await svc.prepareForSession();
      });
      await pump(tester, svc);

      expect(find.text(kEncryptionAwaitingApprovalMessage), findsOneWidget);
    });

    testWidgets('hidden when disabled or unlocked, and follows the state',
        (tester) async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await pump(tester, svc);
      expect(find.byType(MaterialBanner), findsNothing);

      await tester.runAsync(() => svc.enable(const RecoveryKeyChoice()));
      await tester.pump();
      expect(find.byType(MaterialBanner), findsNothing);

      svc.lock();
      await tester.pump();
      expect(find.byType(MaterialBanner), findsOneWidget);
    });

    testWidgets('Recover access opens the recovery screen', (tester) async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await tester.runAsync(() => svc.enable(const RecoveryKeyChoice()));
      svc.lock();
      await pump(tester, svc);

      await tester.tap(find.text('Recover access'));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(AppBar, 'Recover access'), findsOneWidget);
    });
  });
}
