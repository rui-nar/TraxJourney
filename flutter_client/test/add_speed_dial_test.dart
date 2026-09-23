// Behaviour of the add-FAB speed dial: collapsed by default, fans the actions
// out on tap, and firing one runs its callback and collapses again.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/add_speed_dial.dart';

void main() {
  testWidgets('fans actions out on tap and fires the selected one',
      (tester) async {
    final fired = <String>[];

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        floatingActionButton: AddSpeedDial(
          actions: [
            SpeedDialAction(
                icon: Icons.photo_camera_outlined,
                label: 'Memory',
                onTap: () => fired.add('memory')),
            SpeedDialAction(
                icon: Icons.edit_calendar_outlined,
                label: 'Day info',
                onTap: () => fired.add('day')),
            SpeedDialAction(
                icon: Icons.add_chart_outlined,
                label: 'Counter',
                onTap: () => fired.add('counter')),
          ],
        ),
      ),
    ));

    // Collapsed: action labels aren't in the tree at all.
    expect(find.text('Memory'), findsNothing);
    expect(find.text('Counter'), findsNothing);

    // Open the dial (main FAB shows the + icon).
    await tester.tap(find.byIcon(Icons.add));
    await tester.pumpAndSettle();

    expect(find.text('Memory'), findsOneWidget);
    expect(find.text('Day info'), findsOneWidget);
    expect(find.text('Counter'), findsOneWidget);

    // Pick "Counter" via its mini-FAB icon.
    await tester.tap(find.byIcon(Icons.add_chart_outlined));
    await tester.pumpAndSettle();

    expect(fired, ['counter']);
    // Collapses again after selection.
    expect(find.text('Counter'), findsNothing);
  });
}
