// Regression test for a pan-jerkiness/ANR finding: TrackMapEditor's
// onPositionChanged callback used to call setState(() => _bounds = ...)
// directly, and flutter_map fires that callback on every camera frame during
// a drag — each one forcing a full rescan of every point in the track
// (_visibleVertexIndices) plus reconstruction of a handle Marker per visible
// vertex. On a thousands-point track that's enough synchronous work per
// frame to ANR. Fixed by debouncing the _bounds commit to 120ms after
// panning actually pauses.

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

import 'package:traxjourney_client/src/projects/track_edit_model.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';
import 'package:traxjourney_client/src/projects/track_map_editor.dart';

MapCamera _cameraAt(double lat, double lng, double zoom) => MapCamera(
      crs: const Epsg3857(),
      center: LatLng(lat, lng),
      zoom: zoom,
      rotation: 0,
      nonRotatedSize: const Size(800, 600),
    );

/// 50 points tightly clustered near (0, 0) plus 50 tightly clustered near
/// (10, 10) — a close zoom centred on the first cluster excludes the second,
/// a wide-out zoom centred between them includes both.
List<EditPoint> _points() => [
      for (var i = 0; i < 50; i++) EditPoint(i * 0.00001, i * 0.00001),
      for (var i = 0; i < 50; i++)
        EditPoint(10.0 + i * 0.00001, 10.0 + i * 0.00001),
    ];

Widget _editor(TrackEditorController controller) => MaterialApp(
      home: Scaffold(
        body: TrackMapEditor(
          controller: controller,
          actionsForPoint: (_) => const [],
          pointMenuHint: '',
        ),
      ),
    );

void main() {
  testWidgets(
      'onPositionChanged is debounced: a burst of pan-frame events does not '
      'each commit the bounds-filtered handle-marker rebuild — only once '
      'panning pauses for 120ms', (tester) async {
    tester.view.physicalSize = const Size(800, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final model = TrackEditModel.fromPoints(_points());
    final controller = TrackEditorController(model);
    addTearDown(controller.dispose);

    await tester.pumpWidget(_editor(controller));
    await tester.pump();

    int markerCount() =>
        tester.widget<MarkerLayer>(find.byType(MarkerLayer)).markers.length;

    // Before any position-changed event has ever landed, _bounds is null and
    // every vertex gets a handle — both clusters present.
    expect(markerCount(), 100);

    final onPositionChanged = tester
        .widget<FlutterMap>(find.byType(FlutterMap))
        .options
        .onPositionChanged!;

    onPositionChanged(_cameraAt(0.0002, 0.0002, 12), true);
    await tester.pump(const Duration(milliseconds: 150));
    expect(markerCount(), 50,
        reason: 'a single settled position change filters handles down to '
            'the now-visible near cluster');

    // Simulate a drag: a burst of pan-frame events, each well under the
    // 120ms debounce window apart, panned out far enough that the far
    // cluster would reappear if any single one of them committed.
    for (var i = 0; i < 4; i++) {
      onPositionChanged(_cameraAt(5.0, 5.0, 1), true);
      await tester.pump(const Duration(milliseconds: 40));
    }
    expect(markerCount(), 50,
        reason: 'mid-drag frames must not each commit a rebuild — this is '
            'exactly the unthrottled-setState bug that can ANR a large '
            'track');

    // Panning stops; once 120ms elapse undisturbed the debounce commits.
    await tester.pump(const Duration(milliseconds: 150));
    expect(markerCount(), 100,
        reason: 'the bounds update still lands once panning actually pauses');
  });
}
