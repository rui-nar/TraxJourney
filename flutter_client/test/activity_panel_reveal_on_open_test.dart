import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Regression for issue #21: on narrow layouts the activity panel is built but
/// hidden (slid off-screen). When a segment is selected on the map while the
/// panel is hidden, opening the panel must centre that selection. The panel
/// reveals the current selection when [ActivityPanel.panelVisible] flips
/// false→true, scrolling its injected controller.
class _VisibilityHarness extends StatelessWidget {
  final ProjectNotifier notifier;
  final ScrollController controller;
  final ValueListenable<bool> visible;
  const _VisibilityHarness({
    required this.notifier,
    required this.controller,
    required this.visible,
  });

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        body: ChangeNotifierProvider<ProjectNotifier>.value(
          value: notifier,
          child: ValueListenableBuilder<bool>(
            valueListenable: visible,
            // Mirror the real narrow layout: the panel is always built but slid
            // off-screen while closed. The slide animation keeps frames flowing
            // on open, which is what flushes the panel's post-frame reveal chain.
            builder: (_, v, __) => AnimatedSlide(
              offset: v ? Offset.zero : const Offset(-1, 0),
              duration: const Duration(milliseconds: 280),
              child: SizedBox(
                width: 280,
                height: 600,
                child: ActivityPanel(
                  notifier: notifier,
                  scrollController: controller,
                  panelVisible: v,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

ProjectNotifier _notifierWithSegmentAtEnd() {
  final n = ProjectNotifier(ProjectService());
  final acts = <Map<String, dynamic>>[];
  final items = <Map<String, dynamic>>[];
  for (var i = 0; i < 25; i++) {
    acts.add({
      'id': i,
      'type': 'Run',
      'name': 'Act $i',
      'distance': 1000,
      'moving_time': 600,
      'start_date_local': '2026-06-01T08:00:00',
    });
    items.add({'item_type': 'activity', 'activity_id': i});
  }
  // Dateless segment after the activities → inherits the day, lands last.
  items.add({
    'item_type': 'segment',
    'segment': {'id': 'segZ', 'segment_type': 'train', 'label': 'Train'},
  });
  n.activities = acts;
  n.items = items;
  return n;
}

void main() {
  testWidgets('opening the panel centres the selected segment (issue #21)',
      (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = ScrollController();
    addTearDown(controller.dispose);
    final visible = ValueNotifier<bool>(false);
    addTearDown(visible.dispose);

    final notifier = _notifierWithSegmentAtEnd();
    // Selection made on the map while the panel is hidden.
    notifier.selectedSegmentId = 'segZ';

    await tester.pumpWidget(_VisibilityHarness(
      notifier: notifier, controller: controller, visible: visible));
    await tester.pumpAndSettle();

    // Hidden: controller attached but nothing revealed yet.
    expect(controller.hasClients, isTrue);
    expect(controller.offset, 0);

    // Open the panel → it should expand the day and scroll to the segment.
    visible.value = true;
    await tester.pumpAndSettle();

    expect(controller.offset, greaterThan(0),
        reason: 'panel should scroll to reveal the selected segment on open');
  });

  testWidgets('opening with no selection does not scroll', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final controller = ScrollController();
    addTearDown(controller.dispose);
    final visible = ValueNotifier<bool>(false);
    addTearDown(visible.dispose);

    final notifier = _notifierWithSegmentAtEnd(); // nothing selected

    await tester.pumpWidget(_VisibilityHarness(
      notifier: notifier, controller: controller, visible: visible));
    await tester.pumpAndSettle();

    visible.value = true;
    await tester.pumpAndSettle();

    expect(controller.offset, 0);
  });
}
