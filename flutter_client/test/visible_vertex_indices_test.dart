// Regression tests for a track-editor responsiveness gap: the vertex-handle
// list was filtered to the viewport but never capped. Zoom out to fit a whole
// resolved rail route and every vertex is "in view" — Overpass stores rail
// geometry at full OSM-node resolution, 9,700 points measured for a single
// Hamburg→Offenburg route — so one frame materialised ~9,700 Markers, each
// wrapping a Listener, a GestureDetector and a handle widget.
//
// visibleVertexIndices caps that. The cap must stay *recoverable*: thinning
// is even, so zooming in shrinks the visible run until it fits under the cap
// and every vertex gets its handle back. And it must stay a display
// decision — the controller's points, hence the persisted polyline, are
// never touched.

import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

import 'package:traxjourney_client/src/projects/track_edit_model.dart';
import 'package:traxjourney_client/src/projects/track_map_editor.dart';

/// [n] points marching north-east from (0, 0), one per [step] degrees.
List<EditPoint> _points(int n, {double step = 0.001}) =>
    [for (var i = 0; i < n; i++) EditPoint(i * step, i * step)];

LatLngBounds _boundsOver(List<EditPoint> pts) => LatLngBounds(
      LatLng(pts.first.lat - 1, pts.first.lng - 1),
      LatLng(pts.last.lat + 1, pts.last.lng + 1),
    );

void main() {
  group('visibleVertexIndices', () {
    test('a normal-sized track is unaffected: every visible vertex kept', () {
      final pts = _points(120);
      expect(visibleVertexIndices(pts, _boundsOver(pts)),
          [for (var i = 0; i < 120; i++) i]);
    });

    test('null bounds (before the map reports a camera) keeps everything', () {
      expect(visibleVertexIndices(_points(300), null).length, 300);
    });

    test('a track at exactly the cap is not thinned', () {
      final pts = _points(kMaxVertexHandles);
      expect(visibleVertexIndices(pts, _boundsOver(pts)).length,
          kMaxVertexHandles);
    });

    test('a full-resolution rail route is capped at kMaxVertexHandles', () {
      final pts = _points(9700); // the measured Hamburg→Offenburg worst case
      final visible = visibleVertexIndices(pts, _boundsOver(pts));
      expect(visible.length, kMaxVertexHandles);
    });

    test('the thinned set is ordered, unique, and keeps both ends', () {
      final pts = _points(9700);
      final visible = visibleVertexIndices(pts, _boundsOver(pts));
      expect(visible.first, 0);
      expect(visible.last, 9699);
      for (var i = 1; i < visible.length; i++) {
        expect(visible[i], greaterThan(visible[i - 1]));
      }
    });

    test('vertices dropped at low zoom are reachable by zooming in', () {
      final pts = _points(9700);
      // Vertex 4321 is not among the thinned handles when the whole route
      // is in view...
      const target = 4321;
      expect(visibleVertexIndices(pts, _boundsOver(pts)), isNot(contains(target)));

      // ...but a tighter viewport around it holds fewer vertices than the
      // cap, so every one of them — including 4321 — gets a handle back.
      final p = pts[target];
      final zoomed = LatLngBounds(
        LatLng(p.lat - 0.01, p.lng - 0.01),
        LatLng(p.lat + 0.01, p.lng + 0.01),
      );
      final visible = visibleVertexIndices(pts, zoomed);
      expect(visible.length, lessThan(kMaxVertexHandles));
      expect(visible, contains(target));
    });

    test('filtering and thinning never mutate the point list', () {
      final pts = _points(9700);
      final before = [for (final p in pts) (p.lat, p.lng)];
      visibleVertexIndices(pts, _boundsOver(pts));
      expect(pts.length, 9700);
      expect([for (final p in pts) (p.lat, p.lng)], before);
    });

    test('an explicit maxHandles bounds the result', () {
      final pts = _points(1000);
      expect(visibleVertexIndices(pts, _boundsOver(pts), maxHandles: 10).length,
          10);
    });
  });
}
