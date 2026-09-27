/// Client-side zero-knowledge encryption primitives (issue #26).
///
/// The server never holds the Content Master Key (CMK) and does no crypto; it
/// stores only opaque ciphertext and wrapped keys. This module is the pure,
/// I/O-free core: key generation, the three CMK wraps (recovery key / Q&A /
/// device), and per-field content encryption. Secure-storage and network I/O
/// live in higher layers and are injected, so everything here is unit-testable
/// headless.
///
/// Stack (locked by the Phase 1 spike, see spike/SPIKE_RESULTS.md):
///   AEAD            XChaCha20-Poly1305 (192-bit random nonce)
///   key wrapping    AEAD-wrap (encrypt the key)
///   subkeys         HKDF-SHA256, domain-separated by `info`
///   device wrap     X25519 ECDH -> HKDF -> AEAD
///   Option A key    256-bit CSPRNG
///   Option B (Q&A)  Argon2id over normalized answers
library;

import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography_plus/cryptography_plus.dart';

/// AEAD used for every content/key wrap.
final _aead = Xchacha20.poly1305Aead();
const _nonceLen = 24;
const _macLen = 16;

/// Argon2id parameters for the Option B (Q&A) recovery wrap. Stored alongside
/// the wrap (server `recovery_wrap.kdf_params_json`) so they can evolve.
class Argon2Params {
  final int memoryKib;
  final int iterations;
  final int parallelism;
  const Argon2Params({
    this.memoryKib = 19456, // 19 MiB (OWASP floor); tune per Phase 1 device data
    this.iterations = 2,
    this.parallelism = 1,
  });

  Map<String, int> toJson() => {
        'memoryKib': memoryKib,
        'iterations': iterations,
        'parallelism': parallelism,
      };

  factory Argon2Params.fromJson(Map<String, dynamic> j) => Argon2Params(
        memoryKib: j['memoryKib'] as int,
        iterations: j['iterations'] as int,
        parallelism: j['parallelism'] as int,
      );
}

/// A CMK wrapped under some key. [blob] is the AEAD concatenation
/// (nonce|cipher|mac); [ephemeralPublicKey] is set only for X25519 device wraps.
class WrappedCmk {
  final Uint8List blob;
  final Uint8List? ephemeralPublicKey;
  const WrappedCmk(this.blob, {this.ephemeralPublicKey});
}

// ---------------------------------------------------------------------------
// Core primitives
// ---------------------------------------------------------------------------

/// Generate a fresh random 256-bit Content Master Key.
Future<SecretKey> generateCmk() => _aead.newSecretKey();

Future<Uint8List> _wrapBytes(List<int> plaintext, SecretKey wrapKey) async {
  final box = await _aead.encrypt(plaintext,
      secretKey: wrapKey, nonce: _aead.newNonce());
  return Uint8List.fromList(box.concatenation());
}

Future<List<int>> _unwrapBytes(Uint8List blob, SecretKey wrapKey) async {
  final box =
      SecretBox.fromConcatenation(blob, nonceLength: _nonceLen, macLength: _macLen);
  return _aead.decrypt(box, secretKey: wrapKey);
}

// ---------------------------------------------------------------------------
// Option A — high-entropy recovery key
// ---------------------------------------------------------------------------

/// Generate a random 256-bit recovery secret (the bytes behind a BIP39 phrase
/// / downloadable file).
Uint8List generateRecoverySecret() {
  final rnd = Random.secure();
  return Uint8List.fromList(List<int>.generate(32, (_) => rnd.nextInt(256)));
}

/// Parse a recovery key the user typed back (the grouped hex shown at enable)
/// into raw bytes. Ignores spaces/dashes/case. Null if not 32 bytes of hex.
Uint8List? parseRecoveryKeyHex(String input) {
  final hex = input.replaceAll(RegExp('[^0-9a-fA-F]'), '');
  if (hex.length != 64) return null;
  final bytes = Uint8List(32);
  for (var i = 0; i < 32; i++) {
    bytes[i] = int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16);
  }
  return bytes;
}

Future<SecretKey> _recoveryWrapKey(List<int> recoverySecret, List<int> salt) {
  final hkdf = Hkdf(hmac: Hmac.sha256(), outputLength: 32);
  return hkdf.deriveKey(
    secretKey: SecretKey(recoverySecret),
    nonce: salt,
    info: utf8.encode('viewtrip-e2ee/recovery-wrap/v1'),
  );
}

Future<WrappedCmk> wrapCmkWithRecoveryKey(
    SecretKey cmk, List<int> recoverySecret, List<int> salt) async {
  final wk = await _recoveryWrapKey(recoverySecret, salt);
  return WrappedCmk(await _wrapBytes(await cmk.extractBytes(), wk));
}

Future<SecretKey> unwrapCmkWithRecoveryKey(
    WrappedCmk w, List<int> recoverySecret, List<int> salt) async {
  final wk = await _recoveryWrapKey(recoverySecret, salt);
  return SecretKey(await _unwrapBytes(w.blob, wk));
}

// ---------------------------------------------------------------------------
// Option B — Security Q&A (low-entropy, hardened with Argon2id)
// ---------------------------------------------------------------------------

/// Normalize an answer so legitimate-user variance doesn't break unlock:
/// trim, lowercase, collapse internal whitespace. ("  St. " ~ "st.")
String normalizeAnswer(String raw) =>
    raw.trim().toLowerCase().replaceAll(RegExp(r'\s+'), ' ');

String _combineAnswers(List<String> answers) =>
    answers.map(normalizeAnswer).join(' ');

Future<SecretKey> _qnaWrapKey(
    List<String> answers, List<int> salt, Argon2Params p) {
  final argon2 = Argon2id(
    memory: p.memoryKib,
    parallelism: p.parallelism,
    iterations: p.iterations,
    hashLength: 32,
  );
  return argon2.deriveKeyFromPassword(
    password: _combineAnswers(answers),
    nonce: salt,
  );
}

Future<WrappedCmk> wrapCmkWithQna(SecretKey cmk, List<String> answers,
    List<int> salt, Argon2Params p) async {
  final wk = await _qnaWrapKey(answers, salt, p);
  return WrappedCmk(await _wrapBytes(await cmk.extractBytes(), wk));
}

Future<SecretKey> unwrapCmkWithQna(WrappedCmk w, List<String> answers,
    List<int> salt, Argon2Params p) async {
  final wk = await _qnaWrapKey(answers, salt, p);
  return SecretKey(await _unwrapBytes(w.blob, wk));
}

// ---------------------------------------------------------------------------
// High — passphrase (Argon2id over the RAW passphrase; NOT normalized, because
// case and spacing carry entropy that strengthens a user-chosen passphrase)
// ---------------------------------------------------------------------------

Future<SecretKey> _passphraseWrapKey(
    String passphrase, List<int> salt, Argon2Params p) {
  final argon2 = Argon2id(
    memory: p.memoryKib,
    parallelism: p.parallelism,
    iterations: p.iterations,
    hashLength: 32,
  );
  return argon2.deriveKeyFromPassword(password: passphrase, nonce: salt);
}

Future<WrappedCmk> wrapCmkWithPassphrase(
    SecretKey cmk, String passphrase, List<int> salt, Argon2Params p) async {
  final wk = await _passphraseWrapKey(passphrase, salt, p);
  return WrappedCmk(await _wrapBytes(await cmk.extractBytes(), wk));
}

Future<SecretKey> unwrapCmkWithPassphrase(
    WrappedCmk w, String passphrase, List<int> salt, Argon2Params p) async {
  final wk = await _passphraseWrapKey(passphrase, salt, p);
  return SecretKey(await _unwrapBytes(w.blob, wk));
}

// ---------------------------------------------------------------------------
// Device wrap — X25519 (enables passwordless cross-device approval)
// ---------------------------------------------------------------------------

final _x25519 = X25519();
final _deviceWrapSalt = utf8.encode('viewtrip-e2ee/device-wrap-salt/v1');

Future<SimpleKeyPair> generateDeviceKeyPair() => _x25519.newKeyPair();

Future<SecretKey> _deviceWrapKey(
    SecretKey shared) {
  final hkdf = Hkdf(hmac: Hmac.sha256(), outputLength: 32);
  return hkdf.deriveKey(
    secretKey: shared,
    nonce: _deviceWrapSalt,
    info: utf8.encode('viewtrip-e2ee/device-wrap/v1'),
  );
}

/// Wrap the CMK to a device's PUBLIC key (what "approve a new device" does):
/// anyone holding the CMK can grant access to a device without its private key.
Future<WrappedCmk> wrapCmkToDevicePublicKey(
    SecretKey cmk, SimplePublicKey devicePublicKey) async {
  final ephemeral = await _x25519.newKeyPair();
  final shared = await _x25519.sharedSecretKey(
      keyPair: ephemeral, remotePublicKey: devicePublicKey);
  final wk = await _deviceWrapKey(shared);
  final ephPub = await ephemeral.extractPublicKey();
  return WrappedCmk(
    await _wrapBytes(await cmk.extractBytes(), wk),
    ephemeralPublicKey: Uint8List.fromList(ephPub.bytes),
  );
}

/// Unwrap the CMK with this device's PRIVATE key + the stored ephemeral pubkey.
Future<SecretKey> unwrapCmkWithDeviceKeyPair(
    WrappedCmk w, SimpleKeyPair deviceKeyPair) async {
  final ephPub =
      SimplePublicKey(w.ephemeralPublicKey!, type: KeyPairType.x25519);
  final shared = await _x25519.sharedSecretKey(
      keyPair: deviceKeyPair, remotePublicKey: ephPub);
  final wk = await _deviceWrapKey(shared);
  return SecretKey(await _unwrapBytes(w.blob, wk));
}

// ---------------------------------------------------------------------------
// Per-field content encryption (random DEK wrapped by the CMK)
// ---------------------------------------------------------------------------

/// Self-describing ciphertext for one text field. Serialized to base64 and
/// stored in the existing DB column when the row's enc_version >= 1.
class EncryptedField {
  /// Crypto scheme version, mirrored into the row's enc_version.
  static const int version = 1;

  final Uint8List wrappedDek; // DEK encrypted under the CMK
  final Uint8List ciphertext; // field plaintext encrypted under the DEK
  const EncryptedField(this.wrappedDek, this.ciphertext);

  /// Compact base64 envelope: `v1.{wrappedDek}.{ciphertext}`.
  String encode() =>
      'v$version.${base64.encode(wrappedDek)}.${base64.encode(ciphertext)}';

  static EncryptedField decode(String s) {
    final parts = s.split('.');
    if (parts.length != 3 || parts[0] != 'v$version') {
      throw FormatException('Unrecognized encrypted field envelope', s);
    }
    return EncryptedField(base64.decode(parts[1]), base64.decode(parts[2]));
  }

  /// Cheap structural check: does this string look like an encrypted envelope?
  /// (Plaintext like "v1.2 notes" splits into 2 parts, so it's not mistaken.)
  static bool isEnvelope(String s) {
    final parts = s.split('.');
    return parts.length == 3 && parts[0] == 'v$version';
  }

  /// Strict check: could [s] be an envelope [encode] produced? Both parts are
  /// base64, the wrapped DEK is exactly nonce + 32-byte key + MAC, and the
  /// ciphertext holds at least a nonce and a MAC. Text a user typed, such as
  /// "v1.2.3" or "v1.abcd.efgh", passes [isEnvelope] but fails this.
  static bool isWellFormed(String s) {
    if (!isEnvelope(s)) return false;
    final parts = s.split('.');
    try {
      return base64.decode(parts[1]).length == _nonceLen + 32 + _macLen &&
          base64.decode(parts[2]).length >= _nonceLen + _macLen;
    } on FormatException {
      return false;
    }
  }
}

/// Encrypt a text field: random DEK, wrap DEK under CMK, encrypt text under DEK.
Future<EncryptedField> encryptField(String plaintext, SecretKey cmk) async {
  final dek = await _aead.newSecretKey();
  final wrappedDek = await _wrapBytes(await dek.extractBytes(), cmk);
  final box = await _aead.encrypt(utf8.encode(plaintext),
      secretKey: dek, nonce: _aead.newNonce());
  return EncryptedField(wrappedDek, Uint8List.fromList(box.concatenation()));
}

/// Decrypt a text field. Throws on wrong CMK / tamper.
Future<String> decryptField(EncryptedField f, SecretKey cmk) async {
  final dek = SecretKey(await _unwrapBytes(f.wrappedDek, cmk));
  final box = SecretBox.fromConcatenation(f.ciphertext,
      nonceLength: _nonceLen, macLength: _macLen);
  return utf8.decode(await _aead.decrypt(box, secretKey: dek));
}
