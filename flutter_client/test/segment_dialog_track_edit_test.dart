// Tests for the manual segment-track-edit entry point (issue #150):
//   - "Edit track manually" only shows for an existing train/boat/bus segment.
//   - Tapping it closes the dialog and opens SegmentTrackEditorPage.
//   - Re-resolving a manually edited route confirms before discarding it,
//     and threads force:true into the resolve call once confirmed.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/segment_dialog.dart';
import 'package:traxjourney_client/src/projects/segment_track_editor_page.dart';

Map<String, dynamic> _segment({
  String id = 'seg-1',
  String segmentType = 'boat',
  bool routeEdited = false,
  String routeMode = 'ferry',
}) {
  return {
    'id': id,
    'segment_type': segmentType,
    'label': 'Helsinki -> Tallinn',
    'date': '2026-01-01',
    'start': {'lat': 60.1719, 'lon': 24.9414},
    'end': {'lat': 59.4370, 'lon': 24.7536},
    'route_mode': routeMode,
    'route_status': 'resolved',
    'route_polyline': jsonEncode([
      [24.9414, 60.1719],
      [24.7536, 59.4370],
    ]),
    'route_edited': routeEdited,
  };
}

/// Records updateSegment/resolveTrainRoute calls instead of hitting the network.
class _RecordingNotifier extends ProjectNotifier {
  _RecordingNotifier() : super(ProjectService());

  final List<String?> updatedRouteModes = [];
  final List<({String segId, bool force})> resolveCalls = [];

  @override
  Future<void> updateSegment(
    String segId, {
    required String segmentType,
    required String label,
    required double startLat,
    required double startLon,
    required double endLat,
    required double endLon,
    String? date,
    String? trainNumber,
    String? hafasProvider,
    String? routeMode,
  }) async {
    updatedRouteModes.add(routeMode);
  }

  @override
  Future<Map<String, dynamic>> resolveTrainRoute(
    String segId, {
    String routeMode = 'rail',
    String? hafasProvider,
    String? trainNumber,
    String? date,
    bool force = false,
  }) async {
    resolveCalls.add((segId: segId, force: force));
    return {'route_status': 'resolved', 'stop_count': 2, 'degraded': false};
  }
}

/// Embeds the dialog directly (no Navigator route) — fine for tests that only
/// check widget presence and never exercise a Navigator.pop path.
Widget _inlineHarness(ProjectNotifier notifier, {Map<String, dynamic>? editSegment}) {
  return MaterialApp(
    home: Scaffold(
      body: Center(
        child: SegmentDialog(notifier: notifier, editSegment: editSegment),
      ),
    ),
  );
}

/// Opens the dialog via showDialog, as the real app does (_showSegmentDialog
/// in activity_panel.dart) — needed for tests that pop the dialog, since
/// Navigator.pop only makes sense against a route that was actually pushed.
Future<void> _pumpPushedDialog(
  WidgetTester tester,
  ProjectNotifier notifier, {
  Map<String, dynamic>? editSegment,
}) async {
  // A real size so the pushed SegmentTrackEditorPage's FlutterMap (opened by
  // "Edit track manually") has room to lay out, mirroring
  // activity_editor_page_test.dart's harness.
  tester.view.physicalSize = const Size(1200, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (ctx) => TextButton(
          onPressed: () => showDialog<void>(
            context: ctx,
            builder: (_) =>
                SegmentDialog(notifier: notifier, editSegment: editSegment),
          ),
          child: const Text('open dialog'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open dialog'));
  await tester.pumpAndSettle();
}

void main() {
  group('Edit track manually — visibility', () {
    testWidgets('hidden in create mode', (tester) async {
      final notifier = ProjectNotifier(ProjectService());
      await tester.pumpWidget(_inlineHarness(notifier));
      await tester.pump();
      expect(find.text('Edit track manually'), findsNothing);
    });

    testWidgets('hidden for a flight segment', (tester) async {
      final notifier = ProjectNotifier(ProjectService());
      await tester.pumpWidget(_inlineHarness(
        notifier,
        editSegment: _segment(segmentType: 'flight'),
      ));
      await tester.pump();
      expect(find.text('Edit track manually'), findsNothing);
    });

    testWidgets('shown when editing a boat segment', (tester) async {
      final notifier = ProjectNotifier(ProjectService());
      await tester.pumpWidget(_inlineHarness(
        notifier,
        editSegment: _segment(segmentType: 'boat'),
      ));
      await tester.pump();
      expect(find.text('Edit track manually'), findsOneWidget);
    });

    testWidgets('shown when editing a train segment', (tester) async {
      final notifier = ProjectNotifier(ProjectService());
      await tester.pumpWidget(_inlineHarness(
        notifier,
        editSegment: _segment(segmentType: 'train', routeMode: 'rail'),
      ));
      await tester.pump();
      expect(find.text('Edit track manually'), findsOneWidget);
    });
  });

  testWidgets('tapping it closes the dialog and opens the track editor',
      (tester) async {
    final notifier = _RecordingNotifier();
    await _pumpPushedDialog(tester, notifier,
        editSegment: _segment(segmentType: 'boat'));

    expect(find.byType(SegmentDialog), findsOneWidget);
    await tester.tap(find.text('Edit track manually'));
    await tester.pumpAndSettle();

    expect(find.byType(SegmentDialog), findsNothing);
    expect(find.byType(SegmentTrackEditorPage), findsOneWidget);
  });

  group('re-resolving a manually edited route', () {
    testWidgets('Save prompts to discard the manual edit', (tester) async {
      final notifier = _RecordingNotifier();
      await _pumpPushedDialog(
        tester, notifier,
        editSegment: _segment(routeEdited: true),
      );

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.text('Discard manual route edits?'), findsOneWidget);
      // Nothing sent to the server until the user decides.
      expect(notifier.resolveCalls, isEmpty);
    });

    testWidgets('cancelling leaves the manual edit alone', (tester) async {
      final notifier = _RecordingNotifier();
      await _pumpPushedDialog(
        tester, notifier,
        editSegment: _segment(routeEdited: true),
      );

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      // Two "Cancel" buttons are on screen now — the confirm dialog's own and
      // the SegmentDialog's underneath — so scope to the confirm dialog.
      final confirmDialog = find.ancestor(
        of: find.text('Discard manual route edits?'),
        matching: find.byType(AlertDialog),
      );
      await tester.tap(find.descendant(
        of: confirmDialog, matching: find.text('Cancel')));
      await tester.pumpAndSettle();

      expect(notifier.resolveCalls, isEmpty);
      // The dialog is still open — nothing was discarded.
      expect(find.byType(SegmentDialog), findsOneWidget);
    });

    testWidgets('confirming re-resolves with force:true', (tester) async {
      final notifier = _RecordingNotifier();
      await _pumpPushedDialog(
        tester, notifier,
        editSegment: _segment(id: 'seg-9', routeEdited: true),
      );

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Discard & re-resolve'));
      await tester.pumpAndSettle();

      expect(notifier.resolveCalls, hasLength(1));
      expect(notifier.resolveCalls.single.segId, 'seg-9');
      expect(notifier.resolveCalls.single.force, isTrue);
    });

    testWidgets('a route that was not manually edited resolves without asking',
        (tester) async {
      final notifier = _RecordingNotifier();
      await _pumpPushedDialog(
        tester, notifier,
        editSegment: _segment(routeEdited: false),
      );

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.text('Discard manual route edits?'), findsNothing);
      expect(notifier.resolveCalls, hasLength(1));
      expect(notifier.resolveCalls.single.force, isFalse);
    });
  });
}
