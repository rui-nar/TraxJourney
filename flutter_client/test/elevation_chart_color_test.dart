import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Issue #22: the elevation chart should use the same colour as the map track
/// line by default.
void main() {
  group('effectiveElevationChartColor', () {
    test('auto (unset) → matches the map track colour', () {
      final n = ProjectNotifier(ProjectService());
      n.trackColor = const Color(0xFF112233);
      n.elevationChartColor = null;
      expect(n.effectiveElevationChartColor, const Color(0xFF112233));
    });

    test('explicit override wins over the track colour', () {
      final n = ProjectNotifier(ProjectService());
      n.trackColor = const Color(0xFF112233);
      n.elevationChartColor = const Color(0xFFAABBCC);
      expect(n.effectiveElevationChartColor, const Color(0xFFAABBCC));
    });
  });
}
