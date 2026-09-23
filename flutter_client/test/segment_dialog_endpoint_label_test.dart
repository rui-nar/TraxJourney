// Regression: when a transport segment endpoint is set via "Pick on map"
// (a custom lat/lon with no linked activity), the endpoint dropdown must
// read "Custom" — not "— clear —", which wrongly implies nothing is set.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/segment_dialog.dart';

void main() {
  group('endpointClearOptionLabel', () {
    test('reads "Custom" when a map pin is set with no linked activity', () {
      expect(endpointClearOptionLabel(hasCoords: true), 'Custom');
    });

    test('reads "— clear —" when nothing is set at all', () {
      expect(endpointClearOptionLabel(hasCoords: false), '— clear —');
    });
  });
}
