// Every themed component draws its text in Inter (issues #594, #601).
//
// The themes set `fontFamily` to Inter, but a component whose theme gives it
// a text style of its own (button labels, the app bar title, snackbars) uses
// that style in place of the text theme rather than merging the two. A style
// that names no family then falls back to the platform default: Roboto on
// Android and the web, SF on iOS. This pumps each component under both themes
// and checks the family of every piece of text it draws.
//
// Inter is bundled as one family with a file per weight, so a heavier weight
// on theme text draws the real face. Under google_fonts the theme family held
// only the Regular file and bold was synthesised from it (#601).

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/design_tokens.dart';
import 'package:traxjourney_client/src/core/theme.dart';

import '../helpers/real_fonts.dart';

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

double _width(TextStyle style) {
  final painter = TextPainter(
    text: TextSpan(text: 'Trip settings and members', style: style),
    textDirection: TextDirection.ltr,
  )..layout();
  final width = painter.width;
  painter.dispose();
  return width;
}

void main() {
  setUpAll(loadRealFonts);

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
          if (family != kFontFamily) text: family,
      };
      expect(families.keys, containsAll(
          ['App bar title', 'Elevated', 'Segment A', 'Snack text', 'Body text']));
      expect(notInter, isEmpty, reason: 'drawn outside Inter: $notInter');
    });
  }

  // A synthetic bold keeps the Regular file's advances; each real Inter face
  // is wider than the one below it (#601).
  test('theme text draws the real face at every weight', () {
    final base = lightTheme.textTheme.bodyMedium!.copyWith(fontSize: 20);
    expect(base.fontFamily, kFontFamily);
    final widths = [
      for (final w in [400, 500, 600, 700, 800])
        _width(base.copyWith(fontWeight: FontWeight.values[w ~/ 100 - 1])),
    ];
    for (var i = 1; i < widths.length; i++) {
      expect(widths[i], greaterThan(widths[i - 1]),
          reason: 'w${(i + 4) * 100} measured like w${(i + 3) * 100}: '
              '$widths');
    }
  });
}
