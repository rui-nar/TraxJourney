import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Guards issue #105: tapping a person's phone number in the detail sheet
/// copies it to the clipboard and confirms with a snackbar.
void main() {
  testWidgets('tapping the phone number copies it to the clipboard',
      (tester) async {
    final notifier = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
    final person = {'id': 1, 'name': 'Alice', 'phone': '+1 555-0100'};

    String? copied;
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') {
          copied = (call.arguments as Map)['text'] as String?;
        }
        return null;
      },
    );
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null));

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showPersonDetailSheet(context, notifier, person),
            child: const Text('open'),
          ),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('+1 555-0100'), findsOneWidget);
    await tester.tap(find.text('+1 555-0100'));
    await tester.pump();

    expect(copied, '+1 555-0100');
    expect(find.text('Phone number copied'), findsOneWidget);
  });
}
