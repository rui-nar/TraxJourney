import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/note_field_actions.dart';

/// Guards issue #175: the notes fields get copy/paste buttons that work even
/// when the engine's own selection toolbar doesn't (mobile web).

/// Stands in for the system clipboard. [text] of null models a browser that
/// refused `navigator.clipboard.readText()` — what iOS Safari does.
class _FakeClipboard {
  String? text;
  String? written;

  void install(WidgetTester tester) {
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') {
          written = call.arguments['text'] as String?;
          return null;
        }
        if (call.method == 'Clipboard.getData') {
          return text == null ? null : {'text': text};
        }
        return null;
      },
    );
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null));
  }
}

Future<void> _pump(WidgetTester tester, TextEditingController c) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: TextField(
        controller: c,
        maxLines: 3,
        decoration: InputDecoration(suffixIcon: NoteFieldActions(controller: c)),
      ),
    ),
  ));
}

void main() {
  group('applyPaste', () {
    test('inserts at a collapsed cursor and leaves the cursor after it', () {
      const value = TextEditingValue(
        text: 'hello world',
        selection: TextSelection.collapsed(offset: 5),
      );
      final out = applyPaste(value, ' there');
      expect(out.text, 'hello there world');
      expect(out.selection.baseOffset, 11);
      expect(out.selection.isCollapsed, isTrue);
    });

    test('replaces a selection', () {
      const value = TextEditingValue(
        text: 'hello world',
        selection: TextSelection(baseOffset: 6, extentOffset: 11),
      );
      final out = applyPaste(value, 'there');
      expect(out.text, 'hello there');
      expect(out.selection.baseOffset, 11);
    });

    test('appends when there is no valid selection', () {
      const value = TextEditingValue(text: 'hello');
      final out = applyPaste(value, '!');
      expect(out.text, 'hello!');
      expect(out.selection.baseOffset, 6);
    });
  });

  testWidgets('copy writes the field text to the clipboard', (tester) async {
    final clipboard = _FakeClipboard()..install(tester);
    final c = TextEditingController(text: 'met at the hostel');
    addTearDown(c.dispose);
    await _pump(tester, c);

    await tester.tap(find.byKey(const Key('note-copy')));
    await tester.pumpAndSettle();

    expect(clipboard.written, 'met at the hostel');
    expect(find.text('Note copied'), findsOneWidget);
  });

  testWidgets('copy on an empty field does nothing', (tester) async {
    final clipboard = _FakeClipboard()..install(tester);
    final c = TextEditingController();
    addTearDown(c.dispose);
    await _pump(tester, c);

    await tester.tap(find.byKey(const Key('note-copy')));
    await tester.pumpAndSettle();

    expect(clipboard.written, isNull);
    expect(find.text('Note copied'), findsNothing);
  });

  testWidgets('paste inserts at the cursor', (tester) async {
    _FakeClipboard()
      ..text = 'BRIDGE'
      ..install(tester);
    final c = TextEditingController(text: 'met at the hostel')
      ..selection = const TextSelection.collapsed(offset: 11);
    addTearDown(c.dispose);
    await _pump(tester, c);

    await tester.tap(find.byKey(const Key('note-paste')));
    await tester.pumpAndSettle();

    expect(c.text, 'met at the BRIDGEhostel');
    expect(c.selection.baseOffset, 17);
  });

  testWidgets('a blocked clipboard leaves the text alone and says so',
      (tester) async {
    _FakeClipboard()
      ..text = null // browser refused readText()
      ..install(tester);
    final c = TextEditingController(text: 'unchanged');
    addTearDown(c.dispose);
    await _pump(tester, c);

    await tester.tap(find.byKey(const Key('note-paste')));
    await tester.pumpAndSettle();

    expect(c.text, 'unchanged');
    expect(
      find.text('Nothing to paste, or your browser blocked clipboard access.'),
      findsOneWidget,
    );
  });
}
