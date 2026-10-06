/// No-op on-device cache backend — used on web (and any platform without
/// `dart:io`), where there is no local filesystem to persist a project cache
/// to. `ProjectDataCache` still works there; it just never survives past the
/// current tab's lifetime (in-memory only).
library;

import 'dart:typed_data' show Uint8List;

Future<void> cacheStoreInit() async {}

Future<void> cacheStoreClearAll() async {}

Future<void> cacheStoreDeleteKeyPrefix(String prefix) async {}

Future<Map<String, dynamic>?> cacheStoreRead(String key) async => null;

Future<bool> cacheStoreHasFullGeo(String key) async => false;

Future<({int lockVersion, int schemaVersion, Uint8List? bytes})?>
    cacheStoreReadFullGeoBytes(String key) async => null;

Future<void> cacheStoreWrite(String key, Map<String, dynamic> row) async {}
