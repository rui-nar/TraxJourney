import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/panel_resize.dart';

void main() {
  group('clampPanelWidth', () {
    test('applies the drag delta within bounds', () {
      expect(
        clampPanelWidth(current: 280, dx: 40, available: 1200),
        320,
      );
      expect(
        clampPanelWidth(current: 280, dx: -30, available: 1200),
        250,
      );
    });

    test('never shrinks below the minimum', () {
      expect(
        clampPanelWidth(current: 230, dx: -100, available: 1200),
        kMinPanelWidth,
      );
    });

    test('never grows past the maximum', () {
      expect(
        clampPanelWidth(current: 600, dx: 200, available: 4000),
        kMaxPanelWidth,
      );
    });

    test('reserves minimum map width on a narrow viewport', () {
      // available 700 → panel capped at 700 - 320 = 380.
      expect(
        clampPanelWidth(current: 380, dx: 100, available: 700),
        380,
      );
    });
  });
}
