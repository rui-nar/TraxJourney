// The poster consent dialog's three choices; the barrier is a cancel.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/poster_consent_dialog.dart';

Future<PosterConsentChoice?> _open(WidgetTester tester, String tap) async {
  PosterConsentChoice? result;
  await tester.pumpWidget(MaterialApp(
    home: Builder(
      builder: (ctx) => TextButton(
        onPressed: () async =>
            result = await showPosterConsentDialog(ctx, memoryCount: 3),
        child: const Text('open'),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  if (tap.isNotEmpty) {
    await tester.tap(find.text(tap));
  } else {
    await tester.tapAt(const Offset(2, 2));
  }
  await tester.pumpAndSettle();
  return result;
}

void main() {
  testWidgets('shows the memory count and three choices', (tester) async {
    await tester.pumpWidget(const MaterialApp(
        home: Scaffold(body: PosterConsentDialog(memoryCount: 3))));
    expect(find.textContaining('3 memories'), findsOneWidget);
    expect(find.text('Cancel'), findsOneWidget);
    expect(find.text('Without memory text'), findsOneWidget);
    expect(find.text('Send and generate'), findsOneWidget);
  });

  testWidgets('Send and generate returns sendText', (tester) async {
    expect(await _open(tester, 'Send and generate'),
        PosterConsentChoice.sendText);
  });

  testWidgets('Without memory text returns withoutText', (tester) async {
    expect(await _open(tester, 'Without memory text'),
        PosterConsentChoice.withoutText);
  });

  testWidgets('Cancel returns cancel', (tester) async {
    expect(await _open(tester, 'Cancel'), PosterConsentChoice.cancel);
  });

  testWidgets('dismissing the barrier is a cancel', (tester) async {
    expect(await _open(tester, ''), PosterConsentChoice.cancel);
  });
}
