// PhotoThumbCache — the on-device persistence layer under map_panel.dart's
// _MarkerThumbImage, so a marker thumbnail already fetched on this device
// survives an app restart instead of refetching from scratch every cold
// start. See photo_thumb_cache.dart for the full design rationale.
//
// Under plain `flutter test` there's no real sqflite plugin binding (same
// situation project_cache_store_native.dart already documents), so this only
// guards that the cache degrades to a harmless no-op miss rather than
// throwing — the actual disk round trip is only exercised on a real device.

import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/photo_thumb_cache.dart';

void main() {
  test('read returns null when nothing is cached', () async {
    await photoThumbCache.init();
    expect(await photoThumbCache.read('missing-key'), isNull);
  });

  test('write never throws even without a real store backing it', () async {
    await photoThumbCache.init();
    expect(
      () => photoThumbCache.write('some-key', Uint8List.fromList([1, 2, 3])),
      returnsNormally,
    );
  });

  test('clearAll never throws even without a real store backing it', () async {
    await photoThumbCache.init();
    await expectLater(photoThumbCache.clearAll(), completes);
  });
}
