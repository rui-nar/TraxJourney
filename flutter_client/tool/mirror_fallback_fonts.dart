/// Copies the web engine's fallback fonts into a web build (#601, #602).
///
/// The Flutter web engine draws text the bundled fonts cannot (emoji, CJK,
/// Thai, Arabic, …) with Noto fonts, and loads Roboto at start. By default it
/// downloads them from fonts.gstatic.com, which hands every visitor's address
/// to Google. web/flutter_bootstrap.js points the engine at `fallback-fonts/`
/// in the build instead; this fills that directory with exactly the files the
/// engine can ask for, read from the engine sources of the Flutter SDK that
/// built it, so the list always matches the engine version.
///
///     dart run tool/mirror_fallback_fonts.dart build/web
///
/// Run it after every `flutter build web`. Files already present are kept, so
/// a rerun only fetches what is missing.
library;

import 'dart:io';

/// Where Google serves the files; the engine's own default base URL.
const gstaticBase = 'https://fonts.gstatic.com/s/';

/// The directory in the web build that web/flutter_bootstrap.js names.
const mirrorDir = 'fallback-fonts';

/// The engine sources that list the fallback fonts, relative to the SDK root.
const _engine = 'engine/src/flutter/lib/web_ui/lib/src/engine';

/// Every path under [gstaticBase] the web engine of the SDK at [flutterRoot]
/// can download: the Noto fallback fonts and Roboto.
List<String> fallbackFontPaths(String flutterRoot) {
  final sources = [
    '$flutterRoot/$_engine/font_fallback_data.dart',
    '$flutterRoot/$_engine/canvaskit/fonts.dart',
  ];
  final paths = <String>{};
  final fontPath =
      RegExp(r"[a-z0-9]+/v\d+/[A-Za-z0-9_.-]+\.(?:woff2|ttf|otf)");
  for (final source in sources) {
    final text = File(source).readAsStringSync();
    paths.addAll(fontPath.allMatches(text).map((m) => m[0]!));
  }
  // A changed source layout would otherwise mirror nothing and leave the
  // engine with no fonts to fall back to.
  if (paths.length < 100 || !paths.any((p) => p.startsWith('roboto/'))) {
    throw StateError('Found ${paths.length} fallback font paths in the '
        'engine sources under $flutterRoot; the list moved?');
  }
  return paths.toList()..sort();
}

/// The Flutter SDK running this script: dart sits in bin/cache/dart-sdk/bin.
String _flutterRoot() {
  final fromEnv = Platform.environment['FLUTTER_ROOT'];
  if (fromEnv != null && fromEnv.isNotEmpty) return fromEnv;
  var dir = File(Platform.resolvedExecutable).parent;
  for (var i = 0; i < 4; i++) {
    dir = dir.parent;
  }
  return dir.path;
}

Future<void> _download(HttpClient client, String path, File target) async {
  Object? lastError;
  for (var attempt = 1; attempt <= 3; attempt++) {
    try {
      final request = await client.getUrl(Uri.parse('$gstaticBase$path'));
      final response = await request.close();
      if (response.statusCode != 200) {
        await response.drain<void>();
        throw HttpException('HTTP ${response.statusCode} for $path');
      }
      final partial = File('${target.path}.part');
      await partial.parent.create(recursive: true);
      await response.pipe(partial.openWrite());
      await partial.rename(target.path);
      return;
    } catch (e) {
      lastError = e;
      await Future<void>.delayed(Duration(seconds: attempt));
    }
  }
  throw Exception('Could not download $path: $lastError');
}

Future<void> main(List<String> args) async {
  if (args.length != 1) {
    stderr.writeln('usage: dart run tool/mirror_fallback_fonts.dart <web build>');
    exit(64);
  }
  final out = Directory('${args.single}/$mirrorDir');
  final paths = fallbackFontPaths(_flutterRoot());
  final missing = [
    for (final p in paths)
      if (!File('${out.path}/$p').existsSync()) p,
  ];
  stdout.writeln('${paths.length} fallback fonts, ${missing.length} to fetch '
      'into ${out.path}');

  final client = HttpClient();
  try {
    // Sixteen at a time: polite to the host, and a few seconds in all.
    for (var i = 0; i < missing.length; i += 16) {
      await Future.wait([
        for (final p in missing.skip(i).take(16))
          _download(client, p, File('${out.path}/$p')),
      ]);
    }
  } finally {
    client.close();
  }
  stdout.writeln('Fallback fonts ready in ${out.path}');
}
