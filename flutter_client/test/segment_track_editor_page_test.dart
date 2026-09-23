import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/segment_track_editor_page.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';

Map<String, dynamic> _segment({
  String id = 'seg-1',
  List<List<double>>? routePolyline = const [
    [2.00, 48.0],
    [2.01, 48.0],
    [2.02, 48.0],
    [2.03, 48.0],
  ],
  String segmentType = 'boat',
  double startLat = 48.0,
  double startLon = 2.00,
  double endLat = 48.0,
  double endLon = 2.03,
}) {
  return {
    'id': id,
    'segment_type': segmentType,
    'label': 'Ferry',
    'route_polyline': routePolyline == null ? null : jsonEncode(routePolyline),
    'start': {'lat': startLat, 'lon': startLon},
    'end': {'lat': endLat, 'lon': endLon},
  };
}

/// Captures saveSegmentTrack calls instead of reaching the network.
class _RecordingNotifier extends ProjectNotifier {
  _RecordingNotifier() : super(ProjectService());

  final List<({String segId, Map<String, dynamic> payload})> saves = [];
  bool shouldFail = false;

  @override
  Future<void> saveSegmentTrack(
    String segId, Map<String, dynamic> payload,
  ) async {
    if (shouldFail) throw Exception('save failed');
    saves.add((segId: segId, payload: payload));
  }
}

Future<void> _pump(
  WidgetTester tester,
  Map<String, dynamic> segment, {
  ProjectNotifier? notifier,
}) async {
  tester.view.physicalSize = const Size(1200, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final n = notifier ?? ProjectNotifier(ProjectService());
  await tester.pumpWidget(MaterialApp(
    home: SegmentTrackEditorPage(notifier: n, segment: segment),
  ));
  await tester.pump();
}

/// Push the editor onto a route (as SegmentDialog's "Edit track manually"
/// does) so the page's pop-on-success has a route to return to.
Future<void> _pumpPushed(
  WidgetTester tester,
  Map<String, dynamic> segment,
  ProjectNotifier notifier,
) async {
  tester.view.physicalSize = const Size(1200, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (ctx) => TextButton(
          onPressed: () => Navigator.of(ctx).push(MaterialPageRoute(
            builder: (_) =>
                SegmentTrackEditorPage(notifier: notifier, segment: segment),
          )),
          child: const Text('open editor'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open editor'));
  await tester.pumpAndSettle();
}

TrackEditorController _controllerOf(WidgetTester tester) {
  final state = tester.state<State>(find.byType(SegmentTrackEditorPage));
  // ignore: invalid_use_of_protected_member
  return (state as dynamic).editorControllerForTest as TrackEditorController;
}

void main() {
  group('modelForSegment', () {
    test('decodes the stored route_polyline', () {
      final m = modelForSegment(_segment());
      expect(m.length, 4);
      expect(m.points.first.lat, closeTo(48.0, 1e-6));
      expect(m.points.first.lng, closeTo(2.00, 1e-6));
      expect(m.points.every((p) => p.elev == null), isTrue);
    });

    test('falls back to a straight start/end line with no route yet', () {
      final m = modelForSegment(_segment(
        routePolyline: null,
        startLat: 60.0, startLon: 24.0,
        endLat: 59.0, endLon: 23.0,
      ));
      expect(m.length, 2);
      expect(m.points.first.lat, closeTo(60.0, 1e-6));
      expect(m.points.last.lat, closeTo(59.0, 1e-6));
    });

    test('falls back when the stored route has fewer than 2 points', () {
      final m = modelForSegment(_segment(
        routePolyline: const [[2.0, 48.0]],
        startLat: 60.0, startLon: 24.0,
        endLat: 59.0, endLon: 23.0,
      ));
      expect(m.length, 2);
      expect(m.points.first.lat, closeTo(60.0, 1e-6));
    });
  });

  testWidgets('renders the Add-points toggle, a trim/delete hint, and a disabled Save',
      (tester) async {
    await _pump(tester, _segment());
    expect(find.text('Add points'), findsOneWidget);
    expect(find.textContaining('trim or delete'), findsOneWidget);
    // Unlike the activity editor, a segment route can't be split.
    expect(find.textContaining('split'), findsNothing);

    final save = tester.widget<TextButton>(
      find.ancestor(of: find.text('Save'), matching: find.byType(TextButton)),
    );
    expect(save.onPressed, isNull);
  });

  testWidgets('toggling Add points flips the hint text', (tester) async {
    await _pump(tester, _segment());
    expect(find.textContaining('trim or delete'), findsOneWidget);
    await tester.tap(find.text('Add points'));
    await tester.pump();
    expect(find.textContaining('Tap the map to insert'), findsOneWidget);
  });

  testWidgets('an edit via the controller enables Save', (tester) async {
    await _pump(tester, _segment());
    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    final save = tester.widget<TextButton>(
      find.ancestor(of: find.text('Save'), matching: find.byType(TextButton)),
    );
    expect(save.onPressed, isNotNull);
  });

  testWidgets('removing a point updates the rendered map polyline',
      (tester) async {
    await _pump(tester, _segment());
    PolylineLayer poly() =>
        tester.widget<PolylineLayer>(find.byType(PolylineLayer));
    expect(poly().polylines.first.points.length, 4);

    _controllerOf(tester).removeSelected(1);
    await tester.pump();

    expect(poly().polylines.first.points.length, 3);
  });

  int polyCount(WidgetTester tester) =>
      tester.widget<PolylineLayer>(find.byType(PolylineLayer))
          .polylines
          .first
          .points
          .length;

  testWidgets('Add mode: tapping the map surface inserts a point',
      (tester) async {
    await _pump(tester, _segment());
    expect(polyCount(tester), 4);

    await tester.tap(find.text('Add points'));
    await tester.pump();

    final mapCentre = tester.getCenter(find.byType(FlutterMap));
    await tester.tapAt(mapCentre + const Offset(-200, -120));
    await tester.pump(const Duration(milliseconds: 400));

    expect(polyCount(tester), 5);
  });

  testWidgets('Delete via context menu removes the point and updates the map',
      (tester) async {
    await _pump(tester, _segment());
    expect(polyCount(tester), 4);

    await tester.longPress(find.byKey(const ValueKey('vertex_0')));
    await tester.pumpAndSettle();
    expect(find.text('Delete point'), findsOneWidget);
    // No split/cut actions for a segment route.
    expect(find.text('Split here'), findsNothing);
    expect(find.text('Cut & add transport'), findsNothing);

    await tester.tap(find.text('Delete point'));
    await tester.pumpAndSettle();

    expect(polyCount(tester), 3);
  });

  testWidgets('Trim: keep from here drops the earlier points', (tester) async {
    await _pump(tester, _segment());
    await tester.longPress(find.byKey(const ValueKey('vertex_1')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Trim: keep from here'));
    await tester.pumpAndSettle();

    expect(polyCount(tester), 3);
  });

  testWidgets('Dragging a vertex handle moves the point', (tester) async {
    await _pump(tester, _segment());
    final c = _controllerOf(tester);
    final before = c.points.first;

    await tester.drag(
      find.byKey(const ValueKey('vertex_0')),
      const Offset(0, -80),
    );
    await tester.pump();

    final after = c.points.first;
    expect(after.lat == before.lat && after.lng == before.lng, isFalse);
    expect(c.isDirty, isTrue);
  });

  testWidgets('Save calls notifier.saveSegmentTrack and pops on success',
      (tester) async {
    final notifier = _RecordingNotifier();
    await _pumpPushed(tester, _segment(id: 'seg-42'), notifier);

    _controllerOf(tester).removeSelected(0);
    await tester.pump();

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(notifier.saves, hasLength(1));
    expect(notifier.saves.single.segId, 'seg-42');
    final points = notifier.saves.single.payload['points'] as List;
    expect(points, hasLength(3));
    // The page popped back to the caller.
    expect(find.byType(SegmentTrackEditorPage), findsNothing);
  });

  testWidgets('Save failure shows a snackbar and keeps the page open',
      (tester) async {
    final notifier = _RecordingNotifier()..shouldFail = true;
    await _pumpPushed(tester, _segment(), notifier);

    _controllerOf(tester).removeSelected(0);
    await tester.pump();

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Save failed'), findsOneWidget);
    expect(find.byType(SegmentTrackEditorPage), findsOneWidget);
  });
}
