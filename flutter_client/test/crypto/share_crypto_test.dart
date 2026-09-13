import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';
import 'package:traxjourney_client/src/crypto/share_crypto.dart';

void main() {
  group('generateShareKey', () {
    test('generates distinct keys each call', () async {
      final a = await generateShareKey();
      final b = await generateShareKey();
      expect(await a.extractBytes(), isNot(await b.extractBytes()));
    });
  });

  group('encryptTextWithKey / decryptTextWithKey round-trip', () {
    test('encrypt then decrypt returns the original text', () async {
      final key = await generateShareKey();
      const text = 'A lovely afternoon in Lisbon.';
      final envelope = await encryptTextWithKey(text, key);
      expect(await decryptTextWithKey(envelope, key), text);
    });

    test('envelope has the standard v1.<b64>.<b64> shape', () async {
      final key = await generateShareKey();
      final envelope = await encryptTextWithKey('hello', key);
      expect(EncryptedField.isEnvelope(envelope), isTrue);
    });

    test('wrong key cannot decrypt', () async {
      final key = await generateShareKey();
      final other = await generateShareKey();
      final envelope = await encryptTextWithKey('secret notes', key);
      expect(() => decryptTextWithKey(envelope, other), throwsA(isA<Object>()));
    });

    test('an account CMK-encrypted envelope decrypts fine with a share key'
        ' (key-agnostic — same primitive, no special CMK relationship)', () async {
      final cmk = await generateCmk();
      final field = await encryptField('reused across key kinds', cmk);
      // Decrypting a CMK-produced envelope directly with decryptTextWithKey
      // works because it's the exact same envelope format/primitive.
      expect(await decryptTextWithKey(field.encode(), cmk),
          'reused across key kinds');
    });
  });

  group('shareKeyToBase64 / shareKeyFromBase64 round-trip', () {
    test('round-trips key bytes exactly', () async {
      final key = await generateShareKey();
      final encoded = await shareKeyToBase64(key);
      final decoded = shareKeyFromBase64(encoded);
      expect(await decoded.extractBytes(), await key.extractBytes());
    });

    test('the encoded key still decrypts content after a round-trip', () async {
      final key = await generateShareKey();
      final envelope = await encryptTextWithKey('round-trip me', key);
      final restored = shareKeyFromBase64(await shareKeyToBase64(key));
      expect(await decryptTextWithKey(envelope, restored), 'round-trip me');
    });

    test('is URL-safe (no +, /, or unescaped characters unsafe in a fragment)',
        () async {
      // Run several keys since base64 content is random; URL-safe alphabet
      // never contains '+' or '/'.
      for (var i = 0; i < 20; i++) {
        final key = await generateShareKey();
        final encoded = await shareKeyToBase64(key);
        expect(encoded.contains('+'), isFalse);
        expect(encoded.contains('/'), isFalse);
      }
    });
  });
}
