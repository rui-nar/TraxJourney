// Regression for issue #20: adding a transport segment without a date showed
// the validation error via the root ScaffoldMessenger, so the SnackBar rendered
// behind the modal where the user never saw it. The error must now appear
// INLINE inside the dialog.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/segment_dialog.dart';

Widget _harness(ProjectNotifier notifier) {
  return MaterialApp(
    home: Scaffold(
      body: Center(
        child: SegmentDialog(notifier: notifier), // create mode (no editSegment)
      ),
    ),
  );
}

void main() {
  testWidgets('saving without a date shows the error inline in the dialog',
      (tester) async {
    final notifier = ProjectNotifier(ProjectService()); // empty: no activities → no auto date
    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    // No error before the user tries to save.
    expect(find.text('Set a date for the segment'), findsNothing);

    await tester.tap(find.text('Save'));
    await tester.pump();

    // The message is shown, and it lives INSIDE the AlertDialog (the modal) —
    // not as a SnackBar on the Scaffold behind it.
    final errorFinder = find.text('Set a date for the segment');
    expect(errorFinder, findsOneWidget);
    expect(
      find.descendant(of: find.byType(AlertDialog), matching: errorFinder),
      findsOneWidget,
    );
    // It must NOT be a SnackBar.
    expect(find.widgetWithText(SnackBar, 'Set a date for the segment'), findsNothing);
  });

  testWidgets('the inline error clears once a date is set', (tester) async {
    final notifier = ProjectNotifier(ProjectService());
    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    await tester.tap(find.text('Save'));
    await tester.pump();
    expect(find.text('Set a date for the segment'), findsOneWidget);

    // Open the date picker, pick a day, confirm.
    await tester.tap(find.text('No date set'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('15'));
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();

    expect(find.text('Set a date for the segment'), findsNothing);
  });
}
