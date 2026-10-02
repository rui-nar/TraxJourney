// Guard for issue #526: `dart.library.html` is false under dart2wasm, so a
// conditional import gated on it silently compiles the stub into a --wasm
// build (no Google sign-in button, downloads and the stale-bundle reload doing
// nothing) while the build itself succeeds. Web implementations are selected
// with `dart.library.js_interop` and written against package:web instead of
// the deprecated dart:html.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

final _forbidden = <RegExp, String>{
  RegExp(r'dart\.library\.html'):
      'gate web imports on dart.library.js_interop; dart.library.html is '
          'false under Wasm',
  RegExp(r'''import\s+['"]dart:html['"]'''):
      'use package:web; dart:html is deprecated and does not compile to Wasm',
};

void main() {
  test('no web code is gated on, or written against, dart:html', () {
    final hits = <String>[];
    // `flutter test` runs from the package root.
    for (final e in Directory('lib').listSync(recursive: true)) {
      if (e is! File || !e.path.endsWith('.dart')) continue;
      final lines = e.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        for (final entry in _forbidden.entries) {
          if (entry.key.hasMatch(lines[i])) {
            hits.add('${e.path.replaceAll(r'\', '/')}:${i + 1}: ${entry.value}');
          }
        }
      }
    }
    expect(hits, isEmpty);
  });
}
