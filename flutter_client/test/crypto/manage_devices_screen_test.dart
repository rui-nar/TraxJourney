import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/crypto/manage_devices_screen.dart';

class _FakeStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

/// Minimal device-tracking fake (mirrors the real lifecycle).
class _FakeApi implements EncryptionApi {
  bool enabled = false;
  final Map<String, bool> approved = {}; // pubkey -> approved
  String? wrappedFor; // last pubkey a wrap was uploaded for

  @override
  Future<void> enable(Map<String, dynamic> payload) async {
    enabled = true;
    final d = payload['device'] as Map<String, dynamic>;
    approved[d['public_key'] as String] = true;
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? pub) async => EncryptionStatus(
        enabled: enabled,
        recoveryMethods: const [],
        deviceRegistered: pub != null && approved.containsKey(pub),
        deviceApproved: pub != null && (approved[pub] ?? false),
      );

  @override
  Future<void> registerDevice(String publicKeyB64, String label) async {
    approved.putIfAbsent(publicKeyB64, () => false);
  }

  @override
  Future<List<PendingDevice>> pendingDevices() async => approved.entries
      .where((e) => !e.value)
      .map((e) => PendingDevice(e.key, 'Phone'))
      .toList();

  @override
  Future<void> approveDevice(String pub, String wrapped, String eph) async {
    approved[pub] = true;
    wrappedFor = pub;
  }

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async => null;
}

void main() {
  testWidgets('lists a pending device and approves it', (tester) async {
    final api = _FakeApi();
    // A trusted, unlocked device (enables encryption) drives the screen.
    final svc = EncryptionService(_FakeStore(), api);
    await svc.enable(const RecoveryKeyChoice());
    // A second device registers (real X25519 pubkey, so the wrap is valid).
    final deviceB = await generateDeviceKeyPair();
    final pubB = base64.encode((await deviceB.extractPublicKey()).bytes);
    await api.registerDevice(pubB, 'Phone');

    await tester.pumpWidget(MaterialApp(home: ManageDevicesScreen(service: svc)));
    await tester.pumpAndSettle();

    expect(find.text('Phone'), findsOneWidget);
    expect(find.text('Approve'), findsOneWidget);

    await tester.tap(find.text('Approve'));
    await tester.pumpAndSettle();

    expect(api.wrappedFor, pubB); // CMK was wrapped to device B
    expect(find.text('Approve'), findsNothing); // row removed
    expect(find.textContaining('No devices'), findsOneWidget);
  });

  testWidgets('shows an empty state when nothing is pending', (tester) async {
    final api = _FakeApi();
    final svc = EncryptionService(_FakeStore(), api);
    await svc.enable(const RecoveryKeyChoice());

    await tester.pumpWidget(MaterialApp(home: ManageDevicesScreen(service: svc)));
    await tester.pumpAndSettle();

    expect(find.textContaining('No devices'), findsOneWidget);
  });
}
