import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/crypto/recover_screen.dart';

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

/// Fake that supports enable + recovery-wrap fetch + device re-trust.
class _FakeApi implements EncryptionApi {
  bool enabled = false;
  final _devices = <String, ({bool approved, String? wrap, String? eph})>{};
  RecoveryWrapData? _recoveryKeyWrap;

  @override
  Future<void> enable(Map<String, dynamic> payload) async {
    enabled = true;
    final d = payload['device'] as Map<String, dynamic>;
    _devices[d['public_key'] as String] =
        (approved: true, wrap: d['wrapped_cmk'] as String, eph: d['ephemeral_public_key'] as String);
    final r = payload['recovery'] as Map<String, dynamic>;
    _recoveryKeyWrap = RecoveryWrapData(
        r['wrapped_cmk'] as String, r['salt'] as String, r['kdf_params_json'] as String?);
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? pub) async {
    final d = pub == null ? null : _devices[pub];
    return EncryptionStatus(
      enabled: enabled, recoveryMethods: const [],
      deviceRegistered: d != null, deviceApproved: d?.approved ?? false,
      wrappedCmkB64: d?.wrap, ephemeralPublicKeyB64: d?.eph,
    );
  }

  @override
  Future<void> registerDevice(String publicKeyB64, String label) async =>
      _devices[publicKeyB64] = (approved: false, wrap: null, eph: null);
  @override
  Future<List<PendingDevice>> pendingDevices() async => [];
  @override
  Future<void> approveDevice(String pub, String wrap, String eph) async =>
      _devices[pub] = (approved: true, wrap: wrap, eph: eph);
  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async =>
      method == 'recovery_key' ? _recoveryKeyWrap : null;
}

void main() {
  testWidgets('entering the recovery key restores access', (tester) async {
    final api = _FakeApi();
    // Device A enables encryption and we capture the recovery key it generated.
    final svcA = EncryptionService(_FakeStore(), api);
    final secret = (await svcA.enable(const RecoveryKeyChoice())).recoverySecret!;
    final hex = secret
        .map((b) => b.toRadixString(16).padLeft(2, '0').toUpperCase())
        .join();

    // Fresh deviceless service drives the recover screen.
    final svcC = EncryptionService(_FakeStore(), api);
    await tester.pumpWidget(MaterialApp(home: RecoverScreen(service: svcC)));

    await tester.enterText(find.byType(TextField), hex);
    await tester.tap(find.text('Unlock'));
    await tester.pumpAndSettle();

    expect(find.text('Access restored'), findsOneWidget);
    expect(svcC.isUnlocked, isTrue);
  });

  testWidgets('a wrong recovery key shows an error', (tester) async {
    final api = _FakeApi();
    await EncryptionService(_FakeStore(), api).enable(const RecoveryKeyChoice());

    final svcC = EncryptionService(_FakeStore(), api);
    await tester.pumpWidget(MaterialApp(home: RecoverScreen(service: svcC)));

    await tester.enterText(
        find.byType(TextField), '00' * 32); // valid hex, wrong key
    await tester.tap(find.text('Unlock'));
    await tester.pumpAndSettle();

    expect(find.textContaining("Couldn't unlock"), findsOneWidget);
    expect(svcC.isUnlocked, isFalse);
  });
}
