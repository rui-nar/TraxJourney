import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';

final _salt = Uint8List.fromList(List<int>.generate(16, (i) => i * 7 % 256));
const _params = Argon2Params(memoryKib: 19456, iterations: 2, parallelism: 1);

void main() {
  group('content round-trip via CMK', () {
    test('encrypt then decrypt returns the original text', () async {
      final cmk = await generateCmk();
      const text = 'Sunset over the Dolomites 🏔️ — felt unreal.';
      final enc = await encryptField(text, cmk);
      expect(await decryptField(enc, cmk), text);
    });

    test('wrong CMK cannot decrypt', () async {
      final cmk = await generateCmk();
      final other = await generateCmk();
      final enc = await encryptField('secret', cmk);
      expect(() => decryptField(enc, other), throwsA(isA<Object>()));
    });

    test('tampered ciphertext fails AEAD auth (no silent garbage)', () async {
      final cmk = await generateCmk();
      final enc = await encryptField('secret', cmk);
      enc.ciphertext[enc.ciphertext.length - 1] ^= 0x01;
      expect(() => decryptField(enc, cmk), throwsA(isA<Object>()));
    });
  });

  group('EncryptedField envelope', () {
    test('encode then decode is loss-free and decrypts', () async {
      final cmk = await generateCmk();
      final enc = await encryptField('journal body', cmk);
      final restored = EncryptedField.decode(enc.encode());
      expect(await decryptField(restored, cmk), 'journal body');
    });

    test('a plaintext / malformed value is rejected', () {
      expect(() => EncryptedField.decode('just plain text'),
          throwsA(isA<FormatException>()));
    });

    test('isWellFormed accepts a real envelope and rejects typed look-alikes',
        () async {
      // The backstop behind the editors' "keep this stored envelope" (#466,
      // R3-1): text a user typed can pass isEnvelope, never isWellFormed.
      final cmk = await generateCmk();
      final real = (await encryptField('x', cmk)).encode();
      expect(EncryptedField.isWellFormed(real), isTrue);
      final dek = real.split('.')[1];
      for (final typed in [
        'v1.2.3',
        'v1.Dinner with Dr. Smith',
        'v1.abcd.efgh',
        'v1.$dek.QUJD', // ciphertext too short to hold a nonce and a MAC
        'v1.QUJD.${real.split('.')[2]}', // wrapped key of the wrong size
        'v1.$dek.not base64!',
        'v2.${real.substring(3)}',
      ]) {
        expect(EncryptedField.isWellFormed(typed), isFalse, reason: typed);
      }
    });
  });

  group('Option A — recovery key wrap', () {
    test('wrap then unwrap recovers the CMK', () async {
      final cmk = await generateCmk();
      final secret = generateRecoverySecret();
      final w = await wrapCmkWithRecoveryKey(cmk, secret, _salt);
      final cmk2 = await unwrapCmkWithRecoveryKey(w, secret, _salt);
      final enc = await encryptField('hello', cmk);
      expect(await decryptField(enc, cmk2), 'hello');
    });

    test('wrong recovery key is rejected', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithRecoveryKey(cmk, generateRecoverySecret(), _salt);
      expect(() => unwrapCmkWithRecoveryKey(w, generateRecoverySecret(), _salt),
          throwsA(isA<Object>()));
    });
  });

  group('Option B — Q&A wrap (Argon2id)', () {
    final answers = ['Fluffy', 'Lisbon', 'Mrs. Object'];

    test('wrap then unwrap recovers the CMK', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithQna(cmk, answers, _salt, _params);
      final cmk2 = await unwrapCmkWithQna(w, answers, _salt, _params);
      final enc = await encryptField('hi', cmk);
      expect(await decryptField(enc, cmk2), 'hi');
    });

    test('answer normalization tolerates spacing/case variance', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithQna(cmk, answers, _salt, _params);
      final messy = ['  fluffy ', 'LISBON', 'mrs.   object'];
      final cmk2 = await unwrapCmkWithQna(w, messy, _salt, _params);
      final enc = await encryptField('hi', cmk);
      expect(await decryptField(enc, cmk2), 'hi');
    });

    test('genuinely wrong answers are rejected', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithQna(cmk, answers, _salt, _params);
      expect(() => unwrapCmkWithQna(w, ['x', 'y', 'z'], _salt, _params),
          throwsA(isA<Object>()));
    });
  });

  group('High — passphrase wrap (Argon2id)', () {
    const passphrase = 'correct horse battery staple';

    test('wrap then unwrap recovers the CMK', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithPassphrase(cmk, passphrase, _salt, _params);
      final cmk2 = await unwrapCmkWithPassphrase(w, passphrase, _salt, _params);
      final enc = await encryptField('hi', cmk);
      expect(await decryptField(enc, cmk2), 'hi');
    });

    test('is case-sensitive (unlike Q&A, no normalization)', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithPassphrase(cmk, passphrase, _salt, _params);
      expect(
          () => unwrapCmkWithPassphrase(
              w, 'Correct Horse Battery Staple', _salt, _params),
          throwsA(isA<Object>()));
    });

    test('wrong passphrase is rejected', () async {
      final cmk = await generateCmk();
      final w = await wrapCmkWithPassphrase(cmk, passphrase, _salt, _params);
      expect(() => unwrapCmkWithPassphrase(w, 'nope', _salt, _params),
          throwsA(isA<Object>()));
    });
  });

  group('recovery key hex parsing', () {
    test('round-trips a generated secret regardless of grouping', () {
      final secret = generateRecoverySecret();
      final hex = secret
          .map((b) => b.toRadixString(16).padLeft(2, '0').toUpperCase())
          .join();
      final grouped = [
        for (var i = 0; i < hex.length; i += 4) hex.substring(i, i + 4)
      ].join('-');
      expect(parseRecoveryKeyHex(grouped), secret);
      expect(parseRecoveryKeyHex(hex.toLowerCase()), secret);
    });

    test('rejects non-hex / wrong length', () {
      expect(parseRecoveryKeyHex('not a key'), isNull);
      expect(parseRecoveryKeyHex('ABCD'), isNull);
    });
  });

  group('Device wrap — X25519 (cross-device re-wrap)', () {
    test('CMK wrapped to a device public key unwraps with its private key',
        () async {
      final cmk = await generateCmk();
      final device = await generateDeviceKeyPair();
      final pub = await device.extractPublicKey();
      final w = await wrapCmkToDevicePublicKey(cmk, pub);
      final cmk2 = await unwrapCmkWithDeviceKeyPair(w, device);
      final enc = await encryptField('x', cmk);
      expect(await decryptField(enc, cmk2), 'x');
    });

    test('another device cannot unwrap', () async {
      final cmk = await generateCmk();
      final deviceA = await generateDeviceKeyPair();
      final deviceB = await generateDeviceKeyPair();
      final pubA = await deviceA.extractPublicKey();
      final w = await wrapCmkToDevicePublicKey(cmk, pubA);
      expect(() => unwrapCmkWithDeviceKeyPair(w, deviceB),
          throwsA(isA<Object>()));
    });
  });
}
