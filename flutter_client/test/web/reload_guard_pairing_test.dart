// Guard for the web shell's reload-guard pairing (issue #151, the
// TraxJourney rename). web/reload_guard.js publishes its factory on a global,
// and web/index.html looks that global up by name. The lookup is wrapped in
// `if (window.<name>)`, so renaming only one side throws nothing: Android PWA
// black-screen recovery just silently stops running. Both files must agree.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  group('reload guard global', () {
    // `flutter test` runs from the package root.
    final js = File('web/reload_guard.js').readAsStringSync();
    final html = File('web/index.html').readAsStringSync();

    /// The global reload_guard.js assigns in its browser (non-CommonJS) branch.
    String? publishedName() =>
        RegExp(r'root\.([A-Za-z_$][\w$]*)\s*=\s*factory\(\)')
            .firstMatch(js)
            ?.group(1);

    /// Every `window.<name>` the inline script right after the
    /// reload_guard.js include reads.
    Set<String> consumedNames() {
      final block = RegExp(
        r'<script src="reload_guard\.js"></script>\s*<script>([\s\S]*?)</script>',
      ).firstMatch(html)?.group(1);
      if (block == null) return {};
      return RegExp(r'window\.([A-Za-z_$][\w$]*)')
          .allMatches(block)
          .map((m) => m.group(1)!)
          .toSet();
    }

    test('reload_guard.js publishes a named global', () {
      expect(publishedName(), isNotNull);
      expect(publishedName(), isNotEmpty);
    });

    test('index.html loads reload_guard.js and calls it through a global', () {
      expect(consumedNames(), isNotEmpty);
    });

    test('index.html reads exactly the global reload_guard.js publishes', () {
      expect(consumedNames(), {publishedName()});
    });
  });
}
