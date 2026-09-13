import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/basemaps.dart';

void main() {
  group('map zoom cap', () {
    // vector_map_tiles' _ZoomScaler precomputes a fixed 24-entry table (zoom
    // levels 0..23) and indexes it directly by tile zoom with no clamp. The
    // tile zoom is floor(cameraZoom + tileOffset), and the mapbox offset is -1,
    // so the table is safe as long as floor(maxZoom - 1) <= 23, i.e. the camera
    // cap stays below 24. If anyone bumps kMaxMapZoom past this, the interactive
    // map crashes with a RangeError on pinch-zoom — this test guards that.
    const scalerTableSize = 24;

    test('stays below the vector tile scaler limit', () {
      expect(kMaxMapZoom, lessThan(scalerTableSize));
    });

    test('derived tile zoom never exceeds the scaler table bounds', () {
      const mapboxZoomOffset = -1;
      final maxTileZoom = (kMaxMapZoom + mapboxZoomOffset).floor();
      expect(maxTileZoom, lessThan(scalerTableSize));
      expect(maxTileZoom, greaterThanOrEqualTo(0));
    });

    test('is high enough to remain useful', () {
      expect(kMaxMapZoom, greaterThanOrEqualTo(18));
    });
  });
}
