import 'dart:async';
import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';

/// In-memory device key store (the real one persists to the OS keystore).
class FakeDeviceKeyStore implements DeviceKeyStore {
  SimpleKeyPair? _kp;
  @override
  Future<SimpleKeyPair?> load() async => _kp;
  @override
  Future<void> save(SimpleKeyPair keyPair) async => _kp = keyPair;
}

class _FakeDev {
  final bool approved;
  final String? wrappedCmk;
  final String? ephemeral;
  _FakeDev(this.approved, this.wrappedCmk, this.ephemeral);
}

/// In-memory fake server: tracks devices (pubkey -> state) so the full
/// enable / register / approve / unlock lifecycle runs end to end.
class FakeEncryptionApi implements EncryptionApi {
  Map<String, dynamic>? enablePayload;
  bool _enabled = false;
  final _devices = <String, _FakeDev>{};
  final _recovery = <String>[];
  final _recoveryWraps = <String, RecoveryWrapData>{};

  @override
  Future<void> enable(Map<String, dynamic> payload) async {
    enablePayload = payload;
    _enabled = true;
    final d = payload['device'] as Map<String, dynamic>;
    _devices[d['public_key'] as String] = _FakeDev(
        true, d['wrapped_cmk'] as String, d['ephemeral_public_key'] as String);
    final r = payload['recovery'] as Map<String, dynamic>;
    _recovery.add(r['method'] as String);
    _recoveryWraps[r['method'] as String] = RecoveryWrapData(
        r['wrapped_cmk'] as String, r['salt'] as String,
        r['kdf_params_json'] as String?);
  }

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async =>
      _recoveryWraps[method];

  @override
  Future<EncryptionStatus> fetchStatus(String? devicePublicKeyB64) async {
    final dev = devicePublicKeyB64 == null ? null : _devices[devicePublicKeyB64];
    return EncryptionStatus(
      enabled: _enabled,
      recoveryMethods: List.of(_recovery),
      deviceRegistered: dev != null,
      deviceApproved: dev?.approved ?? false,
      wrappedCmkB64: dev?.wrappedCmk,
      ephemeralPublicKeyB64: dev?.ephemeral,
    );
  }

  @override
  Future<void> registerDevice(String publicKeyB64, String label) async {
    _devices.putIfAbsent(publicKeyB64, () => _FakeDev(false, null, null));
  }

  @override
  Future<List<PendingDevice>> pendingDevices() async => _devices.entries
      .where((e) => !e.value.approved)
      .map((e) => PendingDevice(e.key, ''))
      .toList();

  @override
  Future<void> approveDevice(
      String publicKeyB64, String wrappedCmkB64, String ephemeralPublicKeyB64) async {
    _devices[publicKeyB64] = _FakeDev(true, wrappedCmkB64, ephemeralPublicKeyB64);
  }
  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {}
  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async =>
      wrappedCmkB64;
}

/// Holds the enable call until [gate] completes; [called] completes when it
/// arrives.
class _SlowEnableApi extends FakeEncryptionApi {
  final called = Completer<void>();
  final gate = Completer<void>();

  @override
  Future<void> enable(Map<String, dynamic> payload) async {
    called.complete();
    await gate.future;
    return super.enable(payload);
  }
}

/// Holds every key-pair load until [gate] completes; [called] completes when
/// the first arrives.
class _SlowLoadStore extends FakeDeviceKeyStore {
  final called = Completer<void>();
  final gate = Completer<void>();

  @override
  Future<SimpleKeyPair?> load() async {
    if (!called.isCompleted) called.complete();
    await gate.future;
    return super.load();
  }
}

/// Holds every recovery-wrap answer until [gate] completes.
class _SlowRecoveryApi extends FakeEncryptionApi {
  final gate = Completer<void>();

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async {
    await gate.future;
    return super.fetchRecoveryWrap(method);
  }
}

/// Holds every status answer until [gate] completes.
class _SlowStatusApi extends FakeEncryptionApi {
  final gate = Completer<void>();

  @override
  Future<EncryptionStatus> fetchStatus(String? devicePublicKeyB64) async {
    await gate.future;
    return super.fetchStatus(devicePublicKeyB64);
  }
}

/// A Decision 16 server: a `recovery_key` wrap starts unconfirmed, confirm and
/// replace are compare-and-sets, and enable answers with the stored wrap.
class _RecoveryServerApi extends FakeEncryptionApi {
  final unconfirmed = <String>{};
  final confirmCalls = <String>[];
  final replaceCalls = <String>[];
  int statusCalls = 0;

  /// When set, confirm throws it instead of answering.
  Object? confirmError;

  /// The replaced `recovery_key` wrap and its salt, once there is one.
  RecoveryWrapData? _replaced;

  @override
  Future<String?> enable(Map<String, dynamic> payload) async {
    await super.enable(payload);
    final r = payload['recovery'] as Map<String, dynamic>;
    if (r['method'] == 'recovery_key') unconfirmed.add('recovery_key');
    return r['wrapped_cmk'] as String;
  }

  @override
  Future<EncryptionStatus> fetchStatus(String? devicePublicKeyB64) async {
    statusCalls++;
    final s = await super.fetchStatus(devicePublicKeyB64);
    return EncryptionStatus(
      enabled: s.enabled,
      recoveryMethods: s.recoveryMethods,
      deviceRegistered: s.deviceRegistered,
      deviceApproved: s.deviceApproved,
      wrappedCmkB64: s.wrappedCmkB64,
      ephemeralPublicKeyB64: s.ephemeralPublicKeyB64,
      unconfirmedRecoveryMethods: unconfirmed.toList(),
    );
  }

  @override
  Future<RecoveryWrapData?> fetchRecoveryWrap(String method) async {
    final replaced = _replaced;
    if (method == 'recovery_key' && replaced != null) return replaced;
    return super.fetchRecoveryWrap(method);
  }

  @override
  Future<void> confirmRecovery(String method, String wrappedCmkB64) async {
    confirmCalls.add(wrappedCmkB64);
    if (confirmError != null) throw confirmError!;
    final stored = await fetchRecoveryWrap(method);
    if (stored?.wrappedCmkB64 != wrappedCmkB64) throw const RecoveryKeyConflict();
    unconfirmed.remove(method);
  }

  @override
  Future<String> replaceRecoveryKey(String wrappedCmkB64, String saltB64) async {
    replaceCalls.add(wrappedCmkB64);
    if (!unconfirmed.contains('recovery_key')) throw const RecoveryKeyConflict();
    _replaced = RecoveryWrapData(wrappedCmkB64, saltB64, null);
    return wrappedCmkB64;
  }
}

/// A trusted device that turned encryption on with a recovery key it never
/// confirmed, signed in again on [api]: unlocked, with the replacement due.
Future<EncryptionService> _signedInUnconfirmed(_RecoveryServerApi api) async {
  final store = FakeDeviceKeyStore();
  await EncryptionService(store, api).enable(const RecoveryKeyChoice());
  final svc = EncryptionService(store, api);
  expect(await svc.prepareForSession(), isTrue);
  return svc;
}

void main() {
  group('enable', () {
    test('Option A returns a one-time recovery secret and posts a valid payload',
        () async {
      final api = FakeEncryptionApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api, deviceLabel: 'Test');
      final result = await svc.enable(const RecoveryKeyChoice());

      expect(result.recoverySecret, isNotNull);
      expect(result.recoverySecret!.length, 32);
      expect(svc.isUnlocked, isTrue);

      final payload = api.enablePayload!;
      expect((payload['recovery'] as Map)['method'], 'recovery_key');
      expect((payload['recovery'] as Map)['kdf_params_json'], isNull);
      final device = payload['device'] as Map;
      // all blobs are valid base64
      for (final k in ['public_key', 'wrapped_cmk', 'ephemeral_public_key']) {
        expect(() => base64.decode(device[k] as String), returnsNormally);
      }
      expect(device['label'], 'Test');
    });

    test('Medium (Q&A) posts qna, persists chosen questions, no recovery secret',
        () async {
      final api = FakeEncryptionApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final result = await svc.enable(const QnaChoice(
          ['Pet?', 'City?', 'School?'], ['Fluffy', 'Lisbon', 'Hillcrest']));

      expect(result.recoverySecret, isNull);
      expect(svc.isUnlocked, isTrue);
      final recovery = api.enablePayload!['recovery'] as Map;
      expect(recovery['method'], 'qna');
      expect(recovery['kdf_params_json'], isNotNull);
      // The chosen questions are persisted (non-secret) for the recovery screen.
      expect(await svc.qnaRecoveryQuestions(), ['Pet?', 'City?', 'School?']);
    });

    test('High (passphrase) posts passphrase + params and no recovery secret', () async {
      final api = FakeEncryptionApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final result =
          await svc.enable(const PassphraseChoice('correct horse battery staple'));

      expect(result.recoverySecret, isNull);
      expect(svc.isUnlocked, isTrue);
      final recovery = api.enablePayload!['recovery'] as Map;
      expect(recovery['method'], 'passphrase');
      expect(recovery['kdf_params_json'], isNotNull);
    });
  });

  group('unlock', () {
    test('a trusted device unlocks the CMK in a fresh session and round-trips',
        () async {
      final api = FakeEncryptionApi();
      final store = FakeDeviceKeyStore(); // persists across "sessions"

      // Session 1: enable + encrypt something.
      final svc1 = EncryptionService(store, api);
      await svc1.enable(const RecoveryKeyChoice());
      final envelope = await svc1.encryptText('Honeymoon 2025');

      // Session 2: a brand-new service over the SAME store unlocks via the device.
      final svc2 = EncryptionService(store, api);
      expect(svc2.isUnlocked, isFalse);
      expect(await svc2.unlock(), isTrue);
      expect(await svc2.decryptText(envelope), 'Honeymoon 2025');
    });

    test('no stored device key -> cannot unlock', () async {
      final api = FakeEncryptionApi()..enablePayload = null;
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svc.unlock(), isFalse);
    });

    test(
        'a lock() while enable() is still building keys sends and saves '
        'nothing (U5-R3-1)', () async {
      final api = FakeEncryptionApi();
      final store = _SlowLoadStore();
      final svc = EncryptionService(store, api);
      final enabling = svc.enable(const RecoveryKeyChoice());
      await store.called.future; // client-side, nothing sent yet
      svc.lock(); // the session ends meanwhile
      store.gate.complete();

      await expectLater(enabling, throwsA(isA<EncryptionSessionEnded>()));
      expect(api.enablePayload, isNull, reason: 'the server was never asked');
      expect(await store.load(), isNull, reason: 'no device key was saved');
      expect(svc.isUnlocked, isFalse);
    });

    test('a lock() while enable() is waiting is not undone (U5-R2-1)',
        () async {
      final api = _SlowEnableApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final enabling = svc.enable(const RecoveryKeyChoice());
      await api.called.future; // now waiting on the server
      svc.lock(); // the session ends meanwhile
      api.gate.complete();

      final result = await enabling;
      expect(svc.isUnlocked, isFalse);
      expect(result.recoverySecret, isNotNull,
          reason: 'the server is enabled; its only recovery secret is kept');
    });

    test('a lock() while recovery is waiting is not undone (U5-R2-1)',
        () async {
      final api = _SlowRecoveryApi();
      final secret = (await EncryptionService(FakeDeviceKeyStore(), api)
              .enable(const RecoveryKeyChoice()))
          .recoverySecret!;

      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final recovering = svc.recoverWithRecoveryKey(secret);
      await pumpEventQueue(); // now waiting on the server
      svc.lock(); // the session ends meanwhile
      api.gate.complete();

      expect(await recovering, isFalse);
      expect(svc.isUnlocked, isFalse);
    });

    test('a lock() while unlock() is waiting is not undone (U5-R1-2)',
        () async {
      final api = _SlowStatusApi();
      final store = FakeDeviceKeyStore();
      await EncryptionService(store, api).enable(const RecoveryKeyChoice());

      final svc = EncryptionService(store, api);
      final unlocking = svc.unlock();
      await pumpEventQueue(); // now waiting on the server
      svc.lock(); // the session ends meanwhile
      api.gate.complete();

      expect(await unlocking, isFalse);
      expect(svc.isUnlocked, isFalse);
    });

    test('registered but not yet approved -> cannot unlock', () async {
      final api = FakeEncryptionApi();
      // Device A enables encryption (different store).
      await EncryptionService(FakeDeviceKeyStore(), api)
          .enable(const RecoveryKeyChoice());
      // Device B registers and stays pending.
      final svcB = EncryptionService(FakeDeviceKeyStore(), api);
      await svcB.registerThisDevice();
      expect(await svcB.unlock(), isFalse);
    });
  });

  group('cross-device approval', () {
    test('B registers, A approves, B unlocks and reads A\'s ciphertext', () async {
      final api = FakeEncryptionApi();
      final svcA = EncryptionService(FakeDeviceKeyStore(), api);
      await svcA.enable(const RecoveryKeyChoice());
      final envelope = await svcA.encryptText('Trip to Japan');

      final svcB = EncryptionService(FakeDeviceKeyStore(), api);
      await svcB.registerThisDevice();
      expect(await svcB.unlock(), isFalse); // pending

      final pending = await svcA.pendingDevices();
      expect(pending.length, 1);
      await svcA.approveDevice(pending.first.publicKeyB64); // re-wrap CMK to B

      expect(await svcB.unlock(), isTrue);
      expect(await svcB.decryptText(envelope), 'Trip to Japan');
    });

    test('approving requires an unlocked CMK', () async {
      final api = FakeEncryptionApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api); // never unlocked
      expect(() => svc.approveDevice('SOME_PUBKEY'), throwsStateError);
    });

    test('prepareForSession registers a new device as pending when enabled',
        () async {
      final api = FakeEncryptionApi();
      await EncryptionService(FakeDeviceKeyStore(), api)
          .enable(const RecoveryKeyChoice());

      final svcB = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svcB.prepareForSession(), isFalse); // not approved yet
      expect((await api.pendingDevices()).length, 1); // auto-registered
    });
  });

  group('recovery (no trusted device)', () {
    test('recovery key unlocks a fresh device and re-trusts it', () async {
      final api = FakeEncryptionApi();
      final svcA = EncryptionService(FakeDeviceKeyStore(), api);
      final secret = (await svcA.enable(const RecoveryKeyChoice())).recoverySecret!;
      final envelope = await svcA.encryptText('secret note');

      final storeC = FakeDeviceKeyStore(); // brand-new device, no key
      final svcC = EncryptionService(storeC, api);
      expect(await svcC.unlock(), isFalse);

      expect(await svcC.recoverWithRecoveryKey(secret), isTrue);
      expect(await svcC.decryptText(envelope), 'secret note');

      // Re-trusted: a later session on this device unlocks via the device key.
      expect(await EncryptionService(storeC, api).unlock(), isTrue);
    });

    test('passphrase unlocks a fresh device', () async {
      final api = FakeEncryptionApi();
      final svcA = EncryptionService(FakeDeviceKeyStore(), api);
      await svcA.enable(const PassphraseChoice('correct horse battery staple'));
      final envelope = await svcA.encryptText('hi');

      final svcC = EncryptionService(FakeDeviceKeyStore(), api);
      expect(
          await svcC.recoverWithPassphrase('correct horse battery staple'), isTrue);
      expect(await svcC.decryptText(envelope), 'hi');
    });

    test('security questions unlock a fresh device (with persisted questions)',
        () async {
      final api = FakeEncryptionApi();
      final svcA = EncryptionService(FakeDeviceKeyStore(), api);
      await svcA.enable(const QnaChoice(
          ['Pet?', 'City?', 'School?'], ['Fluffy', 'Lisbon', 'Hillcrest']));
      final envelope = await svcA.encryptText('note');

      final svcC = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svcC.qnaRecoveryQuestions(), ['Pet?', 'City?', 'School?']);
      expect(
          await svcC.recoverWithQna(['Fluffy', 'Lisbon', 'Hillcrest']), isTrue);
      expect(await svcC.decryptText(envelope), 'note');
    });

    test('wrong recovery secret fails', () async {
      final api = FakeEncryptionApi();
      await EncryptionService(FakeDeviceKeyStore(), api)
          .enable(const RecoveryKeyChoice());
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svc.recoverWithRecoveryKey(generateRecoverySecret()), isFalse);
    });

    test('recovering an unconfigured method returns false', () async {
      final api = FakeEncryptionApi();
      await EncryptionService(FakeDeviceKeyStore(), api)
          .enable(const RecoveryKeyChoice()); // only recovery_key configured
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await svc.recoverWithQna(['a', 'b', 'c']), isFalse);
    });
  });

  test('encrypt/decrypt throws while locked', () async {
    final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
    expect(() => svc.encryptText('x'), throwsStateError);
  });

  group('protect/reveal (CRUD boundary)', () {
    test('locked: protect and reveal pass through unchanged', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      expect(await svc.protect('hello'), 'hello');
      expect(await svc.reveal('hello'), 'hello');
    });

    test('unlocked: protect produces an envelope; reveal round-trips', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await svc.enable(const RecoveryKeyChoice());

      final protectedVal = await svc.protect('Honeymoon 2025');
      expect(protectedVal, isNot('Honeymoon 2025'));
      expect(EncryptedField.isEnvelope(protectedVal!), isTrue);
      expect(await svc.reveal(protectedVal), 'Honeymoon 2025');
    });

    test('unlocked: reveal leaves plaintext (non-envelope) untouched', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await svc.enable(const RecoveryKeyChoice());
      expect(await svc.reveal('v1.2 release notes'), 'v1.2 release notes');
      expect(await svc.protect(null), isNull);
      expect(await svc.protect(''), '');
    });

    test('unlocked: protect encrypts every value, even one shaped like an envelope',
        () async {
      // Text the user typed ("v1.2.3") and another account's real envelope
      // alike: protect never sends a value as it is (review R2-1 on #466).
      final other = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await other.enable(const RecoveryKeyChoice());
      final foreign = await other.encryptText('not mine');

      final svc = EncryptionService(FakeDeviceKeyStore(), FakeEncryptionApi());
      await svc.enable(const RecoveryKeyChoice());
      for (final value in ['v1.2.3', 'v1.Dinner with Dr. Smith', foreign]) {
        final protectedVal = await svc.protect(value);
        expect(protectedVal, isNot(value));
        expect(await svc.decryptText(protectedVal!), value);
      }
    });
  });

  group('recovery key confirm or replace (Decision 16)', () {
    test('enable hands back the stored wrap to confirm, and confirm records it',
        () async {
      final api = _RecoveryServerApi();
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final result = await svc.enable(const RecoveryKeyChoice());
      final sent = (api.enablePayload!['recovery'] as Map)['wrapped_cmk'];
      expect(result.confirmation!.wrappedCmkB64, sent);

      expect(await svc.confirmRecoveryKey(result.confirmation!),
          RecoveryConfirmOutcome.confirmed);
      expect(api.confirmCalls, [sent]);
      expect(api.unconfirmed, isEmpty);
    });

    test('enable against an older server confirms the wrap it sent', () async {
      final api = FakeEncryptionApi(); // its enable answers nothing
      final svc = EncryptionService(FakeDeviceKeyStore(), api);
      final result = await svc.enable(const RecoveryKeyChoice());
      expect(result.confirmation!.wrappedCmkB64,
          (api.enablePayload!['recovery'] as Map)['wrapped_cmk']);
    });

    test('a passphrase has nothing to confirm', () async {
      final svc = EncryptionService(FakeDeviceKeyStore(), _RecoveryServerApi());
      final result = await svc
          .enable(const PassphraseChoice('correct horse battery staple'));
      expect(result.confirmation, isNull);
    });

    test('an unconfirmed recovery key needs replacing only while unlocked',
        () async {
      final api = _RecoveryServerApi();
      final svc = await _signedInUnconfirmed(api);
      var changes = 0;
      svc.changes.listen((_) => changes++);

      expect(svc.needsRecoveryKeyReplacement, isTrue);
      svc.lock();
      expect(svc.needsRecoveryKeyReplacement, isFalse);
      await pumpEventQueue();
      expect(changes, 1, reason: 'the banner hears the lock');
    });

    test('a status without the field reads as nothing unconfirmed', () async {
      final store = FakeDeviceKeyStore();
      final api = FakeEncryptionApi(); // an older server's status
      await EncryptionService(store, api).enable(const RecoveryKeyChoice());
      final svc = EncryptionService(store, api);
      expect(await svc.prepareForSession(), isTrue);
      expect(svc.needsRecoveryKeyReplacement, isFalse);
    });

    test('a confirmed recovery key is not offered for replacement', () async {
      final store = FakeDeviceKeyStore();
      final api = _RecoveryServerApi();
      final first = EncryptionService(store, api);
      final result = await first.enable(const RecoveryKeyChoice());
      await first.confirmRecoveryKey(result.confirmation!);

      final svc = EncryptionService(store, api);
      expect(await svc.prepareForSession(), isTrue);
      expect(svc.needsRecoveryKeyReplacement, isFalse);
    });

    test('replace sends one wrap, the new key recovers, and confirm clears it',
        () async {
      final api = _RecoveryServerApi();
      final svc = await _signedInUnconfirmed(api);

      final key = await svc.replaceRecoveryKey();
      expect(api.replaceCalls, hasLength(1));
      expect(key.confirmation.wrappedCmkB64, api.replaceCalls.single);
      expect(await svc.confirmRecoveryKey(key.confirmation),
          RecoveryConfirmOutcome.confirmed);
      expect(api.confirmCalls.last, api.replaceCalls.single);
      expect(svc.needsRecoveryKeyReplacement, isFalse);

      // The key shown is the one the server holds.
      final elsewhere = EncryptionService(FakeDeviceKeyStore(), api);
      expect(await elsewhere.recoverWithRecoveryKey(key.secret), isTrue);
    });

    test('a lock() before the replacement is sent sends nothing', () async {
      final api = _RecoveryServerApi();
      final svc = await _signedInUnconfirmed(api);

      final replacing = svc.replaceRecoveryKey(); // still wrapping, client-side
      svc.lock(); // the session ends meanwhile
      await expectLater(replacing, throwsA(isA<EncryptionSessionEnded>()));
      expect(api.replaceCalls, isEmpty);
    });

    test('a confirm after the session ended sends nothing', () async {
      final api = _RecoveryServerApi();
      final svc = await _signedInUnconfirmed(api);
      final key = await svc.replaceRecoveryKey();
      svc.lock();
      expect(await svc.confirmRecoveryKey(key.confirmation),
          RecoveryConfirmOutcome.failed);
      expect(api.confirmCalls, isEmpty);
    });

    test('replacing a confirmed key is refused and refetches the status',
        () async {
      final api = _RecoveryServerApi();
      final svc = await _signedInUnconfirmed(api);
      api.unconfirmed.clear(); // another device confirmed it meanwhile
      final before = api.statusCalls;

      await expectLater(
          svc.replaceRecoveryKey(), throwsA(isA<RecoveryKeyConflict>()));
      expect(api.statusCalls, before + 1);
      expect(svc.needsRecoveryKeyReplacement, isFalse);
    });

    test('confirming a key replaced elsewhere is a conflict (U5b-R1-1)',
        () async {
      final api = _RecoveryServerApi();
      final a = await _signedInUnconfirmed(api);
      final shownOnA = await a.replaceRecoveryKey();
      await a.replaceRecoveryKey(); // stands in for device B's replace
      final before = api.statusCalls;

      expect(await a.confirmRecoveryKey(shownOnA.confirmation),
          RecoveryConfirmOutcome.conflict);
      expect(api.unconfirmed, contains('recovery_key'),
          reason: 'nothing was confirmed');
      expect(api.statusCalls, before + 1, reason: 'the status is refetched');
      expect(a.needsRecoveryKeyReplacement, isTrue);
    });

    test('a failed confirm keeps the key unconfirmed, for the next sign-in',
        () async {
      for (final error in <Object>[
        Exception('405 Method Not Allowed'),
        StateError('network down'),
      ]) {
        final api = _RecoveryServerApi()..confirmError = error;
        final svc = await _signedInUnconfirmed(api);
        final key = await svc.replaceRecoveryKey();
        expect(await svc.confirmRecoveryKey(key.confirmation),
            RecoveryConfirmOutcome.failed);
        expect(svc.needsRecoveryKeyReplacement, isTrue);
      }
    });
  });
}
