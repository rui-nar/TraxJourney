// Pure-function tests (mirrors photo_match_test.dart's style — no widget
// pump needed) for the frame-picker's screen-rect layout and the
// LatLngBounds -> poster-API-bounds conversion (issue #14, unit F).

import 'package:flutter/material.dart' show Size;
import 'package:flutter_map/flutter_map.dart' show LatLngBounds;
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart' show LatLng;
import 'package:traxjourney_client/src/projects/map_panel.dart';

void main() {
  group('frameRectFor', () {
    test('landscape frame is wider than tall', () {
      final rect = frameRectFor(const Size(800, 600), 'landscape');
      expect(rect.width, greaterThan(rect.height));
    });

    test('portrait frame is taller than wide', () {
      final rect = frameRectFor(const Size(800, 600), 'portrait');
      expect(rect.height, greaterThan(rect.width));
    });

    test('landscape frame matches the A0 aspect ratio (1189:841)', () {
      final rect = frameRectFor(const Size(1000, 1000), 'landscape');
      expect(rect.width / rect.height, closeTo(1189 / 841, 0.001));
    });

    test('portrait frame matches the A0 aspect ratio (841:1189)', () {
      final rect = frameRectFor(const Size(1000, 1000), 'portrait');
      expect(rect.width / rect.height, closeTo(841 / 1189, 0.001));
    });

    test('frame is centered within the available size', () {
      const size = Size(800, 600);
      final rect = frameRectFor(size, 'landscape', padding: 20);
      expect(rect.left, closeTo(size.width / 2 - rect.width / 2, 0.001));
      expect(rect.top, closeTo(size.height / 2 - rect.height / 2, 0.001));
    });

    test('frame never exceeds the padded bounds on a narrow viewport', () {
      const size = Size(300, 900);
      final rect = frameRectFor(size, 'landscape', padding: 16);
      expect(rect.width, lessThanOrEqualTo(size.width - 32 + 0.001));
      expect(rect.height, lessThanOrEqualTo(size.height - 32 + 0.001));
    });
  });

  group('posterBoundsFromLatLngBounds', () {
    test('maps LatLngBounds fields to the {north,south,east,west} API shape',
        () {
      final bounds = LatLngBounds(
        const LatLng(10.0, 20.0),
        const LatLng(5.0, 15.0),
      );

      expect(posterBoundsFromLatLngBounds(bounds), {
        'north': 10.0,
        'south': 5.0,
        'east': 20.0,
        'west': 15.0,
      });
    });
  });
}
