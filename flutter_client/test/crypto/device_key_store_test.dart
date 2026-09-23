import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/crypto/device_key_store.dart';
import 'package:traxjourney_client/src/crypto/e2ee_crypto.dart';

class InMemoryKv implements SecureKvStore {
  final Map<String, String> _m = {};
  @override
  Future<String?> read(String key) async => _m[key];
  @override
  Future<void> write(String key, String value) async => _m[key] = value;
  @override
  Future<void> delete(String key) async => _m.remove(key);
}

void main() {
  test('load returns null when nothing stored', () async {
    expect(await SecureStorageDeviceKeyStore(InMemoryKv()).load(), isNull);
  });

  test('saved key pair reloads with identical private key (unwrap fidelity)',
      () async {
    final store = SecureStorageDeviceKeyStore(InMemoryKv());
    final original = await generateDeviceKeyPair();
    await store.save(original);

    // A CMK wrapped to the ORIGINAL public key must unwrap with the RELOADED
    // key pair — proving the seed survived the storage round-trip exactly.
    final cmk = await generateCmk();
    final pub = await original.extractPublicKey();
    final wrapped = await wrapCmkToDevicePublicKey(cmk, pub);

    final reloaded = (await store.load())!;
    final cmk2 = await unwrapCmkWithDeviceKeyPair(wrapped, reloaded);

    final enc = await encryptField('roundtrip', cmk);
    expect(await decryptField(enc, cmk2), 'roundtrip');
  });

  test('clear removes the stored key', () async {
    final kv = InMemoryKv();
    final store = SecureStorageDeviceKeyStore(kv);
    await store.save(await generateDeviceKeyPair());
    await store.clear();
    expect(await store.load(), isNull);
  });
}
