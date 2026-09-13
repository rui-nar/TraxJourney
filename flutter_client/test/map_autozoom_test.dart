import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';

/// Guards issue #34: view-mode auto-zoom must target the SELECTED item, not the
/// whole trip. This exercises the shared bounds helper the auto-zoom uses.
void main() {
  final geo = <String, dynamic>{
    'features': [
      {
        'properties': {'type': 'activity', 'activity_id': '1'},
        'geometry': {
          'coordinates': [
            [10.0, 50.0],
            [10.1, 50.1],
          ],
        },
      },
      {
        'properties': {'type': 'activity', 'activity_id': '2'},
        'geometry': {
          'coordinates': [
            [20.0, 60.0],
            [20.1, 60.1],
          ],
        },
      },
      {
        'properties': {'type': 'segment', 'segment_id': 's9'},
        'geometry': {
          'coordinates': [
            [30.0, 70.0],
            [30.2, 70.2],
          ],
        },
      },
    ],
  };

  test('selecting an activity yields only that activity\'s points', () {
    final pts = ManageMapPanelState.extractSelectedPoints(geo, '1', null, null, null);
    expect(pts, isNotEmpty);
    // Only activity 1 (lat ~50), never activity 2 (lat ~60) — i.e. it zooms to
    // the selection, not the full trip.
    expect(pts.every((p) => p.latitude < 55), isTrue);
    expect(pts.map((p) => p.latitude), containsAll([50.0, 50.1]));
  });

  test('selecting a different activity yields its own points', () {
    final pts = ManageMapPanelState.extractSelectedPoints(geo, '2', null, null, null);
    expect(pts.every((p) => p.latitude > 55 && p.latitude < 65), isTrue);
  });

  test('selecting a segment yields the segment points', () {
    final pts = ManageMapPanelState.extractSelectedPoints(geo, null, 's9', null, null);
    expect(pts, hasLength(2));
    expect(pts.first, const LatLng(70.0, 30.0));
  });

  test('no selection yields no points (nothing to auto-zoom to)', () {
    final pts = ManageMapPanelState.extractSelectedPoints(geo, null, null, null, null);
    expect(pts, isEmpty);
  });
}
