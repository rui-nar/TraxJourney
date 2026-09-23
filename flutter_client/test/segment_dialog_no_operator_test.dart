// The Operator dropdown is gone (issue #277).
//
// It offered db / obb / sncf / sj / dsb / vr / nsb and the server now ignores
// every one of them: train lookups go through one MOTIS/Transitous index that
// covers all of those operators, so the choice changed nothing while telling
// users it mattered. The server still *accepts* and persists `hafas_provider`
// for stored segments and older clients — only the client stops offering and
// sending it.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/segment_dialog.dart';

/// Records whatever the dialog passes for `hafasProvider`, without networking.
class _RecordingNotifier extends ProjectNotifier {
  _RecordingNotifier() : super(ProjectService());

  final List<String?> addProviders = [];
  final List<String?> updateProviders = [];

  @override
  Future<String> addSegment({
    required String segmentType,
    required String label,
    required double startLat,
    required double startLon,
    required double endLat,
    required double endLon,
    int? insertAfterIndex,
    String? date,
    String? trainNumber,
    String? hafasProvider,
  }) async {
    addProviders.add(hafasProvider);
    return 'seg-new';
  }

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
    updateProviders.add(hafasProvider);
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
    return {'route_status': 'resolved', 'stop_count': 2, 'degraded': false};
  }
}

Map<String, dynamic> _trainSegment() => {
      'id': 'seg-1',
      'segment_type': 'train',
      'label': 'Hamburg -> Offenburg',
      'date': '2026-09-01',
      'start': {'lat': 53.5528, 'lon': 10.0067},
      'end': {'lat': 48.4711, 'lon': 7.9447},
      'route_mode': 'rail',
      'route_status': 'resolved',
      'train_number': 'ICE 75',
      // A stored segment still carries the field; the dialog must simply
      // ignore it rather than choke on it.
      'hafas_provider': 'db',
    };

Widget _harness(ProjectNotifier notifier, {Map<String, dynamic>? editSegment}) {
  return MaterialApp(
    home: Scaffold(
      body: Center(
        child: SegmentDialog(notifier: notifier, editSegment: editSegment),
      ),
    ),
  );
}

void main() {
  testWidgets('no Operator control is offered for a rail segment',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 1400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(_harness(
      _RecordingNotifier(),
      editSegment: _trainSegment(),
    ));
    await tester.pumpAndSettle();

    // The rail section is on screen...
    expect(find.text('Train number (optional)'), findsOneWidget);
    // ...but the operator picker and every operator label are gone.
    expect(find.text('Operator'), findsNothing);
    expect(find.byType(DropdownButtonFormField<String>), findsNothing);
    for (final label in const [
      'DB (Deutsche Bahn)',
      'ÖBB (Austria)',
      'SNCF (France)',
      'SJ (Sweden)',
      'DSB (Denmark)',
      'VR (Finland)',
      'NSB / Vy (Norway)',
    ]) {
      expect(find.text(label), findsNothing, reason: '$label must not be shown');
    }
  });

  testWidgets('a stored hafas_provider does not come back on save',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 1400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final notifier = _RecordingNotifier();
    await tester.pumpWidget(_harness(notifier, editSegment: _trainSegment()));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Save'));
    await tester.pump();

    expect(notifier.updateProviders, isNotEmpty);
    // null means the key is omitted from the request body entirely — the
    // plumbing in project_segment_crud_mixin only sends it when non-null.
    expect(notifier.updateProviders.single, isNull);
  });
}
