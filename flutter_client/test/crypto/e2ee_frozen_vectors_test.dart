// Known-answer vectors for the E2EE CMK wraps (issue #26), added as a guard
// for the TraxJourney rename (issue #151).
//
// The HKDF domain-separation strings in lib/src/crypto/e2ee_crypto.dart:
//
//   'viewtrip-e2ee/recovery-wrap/v1'      (recovery-key wrap, HKDF info)
//   'viewtrip-e2ee/device-wrap-salt/v1'   (device wrap, HKDF salt)
//   'viewtrip-e2ee/device-wrap/v1'        (device wrap, HKDF info)
//
// are FROZEN PROTOCOL BYTES, not branding. Every recovery and device wrap a
// user has stored was derived from them; changing a single character makes
// all of those wraps undecryptable — permanent data loss. The round-trip
// tests in e2ee_crypto_test.dart cannot catch that (wrap and unwrap change
// together), so these tests unwrap ciphertext produced once, by the code as
// it stood, and checked in.
//
// NEVER REGENERATE THESE VECTORS. If a test here fails, the code is wrong,
// not the vector. A rename that wants new strings must add a `/v2` scheme
// alongside v1 and keep v1 unwrapping forever.
//
// How they were produced (2026-09-13, e2ee_crypto.dart at 84a156a): the fixed
// CMK below was wrapped with wrapCmkWithRecoveryKey(cmk, secret, salt) and
// wrapCmkToDevicePublicKey(cmk, <public key of the fixed device seed>). The
// AEAD nonce and the ephemeral X25519 key are random, so the blobs are one
// sample each; the derived wrap keys were then computed with the raw
// cryptography_plus primitives.

import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';

List<int> _hex(String s) => [
      for (var i = 0; i < s.length; i += 2)
        int.parse(s.substring(i, i + 2), radix: 16)
    ];

String _toHex(List<int> b) =>
    b.map((x) => x.toRadixString(16).padLeft(2, '0')).join();

// --- Fixed inputs -----------------------------------------------------------

const _cmkHex =
    'c0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedf';
const _recoverySecretHex =
    '5a595c535655484f4241447b7e7d70776a696c636665181f1211140b0e0d0007';
const _saltHex = '10151a1f24292e33383d42474c51565b';
const _deviceSeedHex =
    '80878e959ca3aab1b8bfc6cdd4dbe2e9f0f7fe050c131a21282f363d444b5259';

// --- Stored outputs (nonce|cipher|mac, base64) ------------------------------

const _recoveryBlobB64 =
    '6MKC9el+WadBunlwqFkLp4qVC+CRdcaLuPLz+3CZM4yeuK1oq6HIfxNTlLa4VrNMm/mz7XnO64pJttSAyy79zAyLfqrDx/IP';
const _devicePublicKeyB64 = 'zReUFBRWCbcRolkUYkXuTZ1skq4jOA/c2RfCH0mAmnw=';
const _deviceBlobB64 =
    'GBZtVVOru3z5DKP5bi9YlpUGl5AKO/41DTmyspqrqnOc3ePm95jmnC8XWlah8e/PO29SQgzvZTID/040COqze3s9KgCBpP1g';
const _deviceEphemeralPublicKeyB64 =
    '8x4wBxCzSbysJJjgSOQORCoktaloDrxz6vuyFzjJqHI=';

// --- Derived intermediates --------------------------------------------------

const _recoveryWrapKeyHex =
    '80d488635940527742bbaab6bf9c9426e14c8490b78152db42338eb96216b36b';
const _deviceSharedSecretHex =
    'dd638be2a1b7b09362e818f9fda0a7d7bd6fcadd3d4bc56d30e4e19cfe069970';
const _deviceWrapKeyHex =
    '8971004177faf7ee8870e5c1291d2c2ae90c550f4bd6cc8bd2080c3606837526';

Future<SimpleKeyPair> _deviceKeyPair() =>
    X25519().newKeyPairFromSeed(_hex(_deviceSeedHex));

Future<List<int>> _aeadOpen(List<int> blob, List<int> key) =>
    Xchacha20.poly1305Aead().decrypt(
      SecretBox.fromConcatenation(blob, nonceLength: 24, macLength: 16),
      secretKey: SecretKey(key),
    );

void main() {
  group('frozen vectors — the shipped API still unwraps stored data', () {
    test('recovery-key wrap (recovery-wrap/v1) unwraps to the fixed CMK',
        () async {
      final cmk = await unwrapCmkWithRecoveryKey(
        WrappedCmk(base64.decode(_recoveryBlobB64)),
        _hex(_recoverySecretHex),
        _hex(_saltHex),
      );
      expect(_toHex(await cmk.extractBytes()), _cmkHex);
    });

    test(
        'device wrap (device-wrap-salt/v1 + device-wrap/v1) unwraps to the '
        'fixed CMK', () async {
      final cmk = await unwrapCmkWithDeviceKeyPair(
        WrappedCmk(
          base64.decode(_deviceBlobB64),
          ephemeralPublicKey: base64.decode(_deviceEphemeralPublicKeyB64),
        ),
        await _deviceKeyPair(),
      );
      expect(_toHex(await cmk.extractBytes()), _cmkHex);
    });
  });

  // The v1 scheme spelled out with raw primitives, independent of
  // e2ee_crypto.dart, so a future /v2 author can see exactly what v1 was.
  group('frozen vectors — v1 key-derivation spec', () {
    final hkdf = Hkdf(hmac: Hmac.sha256(), outputLength: 32);

    test('device seed yields the recorded public key', () async {
      final pub = await (await _deviceKeyPair()).extractPublicKey();
      expect(base64.encode(pub.bytes), _devicePublicKeyB64);
    });

    test('recovery wrap key = HKDF-SHA256(secret, salt, recovery-wrap/v1)',
        () async {
      final key = await hkdf.deriveKey(
        secretKey: SecretKey(_hex(_recoverySecretHex)),
        nonce: _hex(_saltHex),
        info: utf8.encode('viewtrip-e2ee/recovery-wrap/v1'),
      );
      final bytes = await key.extractBytes();
      expect(_toHex(bytes), _recoveryWrapKeyHex);
      expect(
          _toHex(await _aeadOpen(base64.decode(_recoveryBlobB64), bytes)),
          _cmkHex);
    });

    test(
        'device wrap key = HKDF-SHA256(X25519 shared, device-wrap-salt/v1, '
        'device-wrap/v1)', () async {
      final shared = await X25519().sharedSecretKey(
        keyPair: await _deviceKeyPair(),
        remotePublicKey: SimplePublicKey(
            base64.decode(_deviceEphemeralPublicKeyB64),
            type: KeyPairType.x25519),
      );
      expect(_toHex(await shared.extractBytes()), _deviceSharedSecretHex);

      final key = await hkdf.deriveKey(
        secretKey: shared,
        nonce: utf8.encode('viewtrip-e2ee/device-wrap-salt/v1'),
        info: utf8.encode('viewtrip-e2ee/device-wrap/v1'),
      );
      final bytes = await key.extractBytes();
      expect(_toHex(bytes), _deviceWrapKeyHex);
      expect(_toHex(await _aeadOpen(base64.decode(_deviceBlobB64), bytes)),
          _cmkHex);
    });
  });
}
