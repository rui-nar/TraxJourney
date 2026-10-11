// The web engine's fallback fonts are served from the build, not Google
// (#601, #602, review R1-1).
//
// By default the engine downloads Roboto at start, and Noto fonts for emoji
// and scripts the bundled fonts lack, from fonts.gstatic.com.
// tool/mirror_fallback_fonts.dart copies them into the build and
// web/flutter_bootstrap.js points the engine at the copy.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import '../../tool/mirror_fallback_fonts.dart';

void main() {
  test('the tool finds every fallback font of this SDK\'s engine', () {
    final root = Platform.environment['FLUTTER_ROOT'];
    expect(root, isNotNull, reason: 'flutter test sets FLUTTER_ROOT');
    final paths = fallbackFontPaths(root!);

    // Roboto (fetched at every start), emoji, and the CJK slices.
    expect(paths.where((p) => p.startsWith('roboto/')), isNotEmpty);
    expect(paths.where((p) => p.startsWith('notocoloremoji/')), isNotEmpty);
    expect(paths.where((p) => p.startsWith('notosansjp/')), isNotEmpty);
    expect(paths.length, greaterThan(500));
    // Plain relative paths: nothing can land outside fallback-fonts/.
    for (final p in paths) {
      expect(p, matches(RegExp(r'^[a-z0-9]+/v\d+/[A-Za-z0-9_.-]+$')));
      expect(p, isNot(contains('..')));
    }
  });

  test('the bootstrap points the engine at the mirrored fonts', () {
    final bootstrap = File('web/flutter_bootstrap.js').readAsStringSync();
    expect(bootstrap,
        contains("fontFallbackBaseUrl: new URL('$mirrorDir/', document.baseURI)"));
    // No absolute URL: the comment names Google's host, the config does not.
    expect(bootstrap, isNot(contains('https://')));
    // The defaults Flutter's own template passes are kept.
    expect(bootstrap, contains('{{flutter_service_worker_version}}'));
  });
}
