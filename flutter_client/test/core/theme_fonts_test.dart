// Every themed component draws its text in Inter (issue #594).
//
// The themes set `fontFamily` to Inter, but a component whose theme gives it
// a text style of its own (button labels, the app bar title, snackbars) uses
// that style in place of the text theme rather than merging the two. A style
// that names no family then falls back to the platform default: Roboto on
// Android and the web, SF on iOS. This pumps each component under both themes
// and checks the family of every piece of text it draws.

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:traxjourney_client/src/core/theme.dart';

/// Every family google_fonts registers for Inter.
final _interFamilies = {
  GoogleFonts.inter().fontFamily,
  for (final w in FontWeight.values) GoogleFonts.inter(fontWeight: w).fontFamily,
};

/// The family of each piece of text drawn under [scope], by the text.
Map<String, String?> _families(WidgetTester tester, Finder scope) {
  final found = <String, String?>{};
  for (final p in tester.renderObjectList<RenderParagraph>(
      find.descendant(of: scope, matching: find.byType(RichText)))) {
    p.text.visitChildren((span) {
      if (span is TextSpan && span.text != null &&
          span.style?.fontFamily != 'MaterialIcons') {
        found[span.text!] = span.style?.fontFamily;
      }
      return true;
    });
  }
  return found;
}

void main() {
  setUpAll(() => GoogleFonts.config.allowRuntimeFetching = false);

  for (final (name, theme) in [('light', lightTheme), ('dark', darkTheme)]) {
    testWidgets('$name theme: every component draws Inter', (tester) async {
      final messenger = GlobalKey<ScaffoldMessengerState>();
      await tester.pumpWidget(MaterialApp(
        theme: theme,
        scaffoldMessengerKey: messenger,
        home: Scaffold(
          appBar: AppBar(title: const Text('App bar title')),
          body: ListView(children: [
            ElevatedButton(onPressed: () {}, child: const Text('Elevated')),
            TextButton(onPressed: () {}, child: const Text('Text button')),
            OutlinedButton(onPressed: () {}, child: const Text('Outlined')),
            FilledButton(onPressed: () {}, child: const Text('Filled')),
            SegmentedButton<int>(
              segments: const [
                ButtonSegment(value: 0, label: Text('Segment A')),
                ButtonSegment(value: 1, label: Text('Segment B')),
              ],
              selected: const {0},
              onSelectionChanged: (_) {},
            ),
            const TextField(decoration: InputDecoration(
                labelText: 'Field label', hintText: 'Field hint')),
            const ListTile(title: Text('Tile title'), subtitle: Text('Tile sub')),
            const Text('Body text'),
          ]),
        ),
      ));
      messenger.currentState!
          .showSnackBar(const SnackBar(content: Text('Snack text')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));

      final families = _families(tester, find.byType(MaterialApp));
      final notInter = {
        for (final MapEntry(key: text, value: family) in families.entries)
          if (!_interFamilies.contains(family)) text: family,
      };
      expect(families.keys, containsAll(
          ['App bar title', 'Elevated', 'Segment A', 'Snack text', 'Body text']));
      expect(notInter, isEmpty, reason: 'drawn outside Inter: $notInter');
    });
  }
}
