// A release build's CanvasKit lives under its engine revision
// (#601 review R3-1).
//
// The server caches canvaskit/ for a day. Under a plain canvaskit/ path a
// visitor returning after a release on a newer Flutter SDK could pair the new
// engine with the cached old CanvasKit; under canvaskit/<revision>/ each
// engine gets its own.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import '../../tool/version_canvaskit.dart';

const _revision = '5d531788691ec3404cac0cee66ead4007b177363';

/// A web build as `flutter build web --no-web-resources-cdn` leaves it.
Directory _build() {
  final dir = Directory.systemTemp.createTempSync('web_build_');
  addTearDown(() => dir.deleteSync(recursive: true));
  File('${dir.path}/flutter_bootstrap.js').writeAsStringSync(
      '_flutter.buildConfig = {"engineRevision":"$_revision",'
      '"useLocalCanvasKit":true};');
  for (final f in ['canvaskit.js', 'canvaskit.wasm', 'chromium/canvaskit.js',
      'chromium/canvaskit.wasm', 'skwasm.js']) {
    File('${dir.path}/canvaskit/$f')
      ..createSync(recursive: true)
      ..writeAsStringSync(f);
  }
  return dir;
}

Set<String> _files(Directory dir) => {
      for (final f in dir.listSync(recursive: true))
        if (f is File) f.path.substring(dir.path.length + 1).replaceAll('\\', '/'),
    };

void main() {
  test('moves every CanvasKit file under the engine revision', () {
    final build = _build();
    versionCanvasKit(build.path);

    expect(_files(Directory('${build.path}/canvaskit')), {
      '$_revision/canvaskit.js',
      '$_revision/canvaskit.wasm',
      '$_revision/chromium/canvaskit.js',
      '$_revision/chromium/canvaskit.wasm',
      '$_revision/skwasm.js',
    });
  });

  test('a second run changes nothing', () {
    final build = _build();
    versionCanvasKit(build.path);
    final before = _files(build);
    versionCanvasKit(build.path);
    expect(_files(build), before);
  });

  // deploy.ps1 rebuilds into the same build/web without `flutter clean`, and
  // Flutter copies canvaskit/ back next to the previous run's moved copy
  // (review R4-2).
  test('a rebuild into the same build replaces the previous run', () {
    final build = _build();
    versionCanvasKit(build.path);
    for (final f in ['canvaskit.js', 'chromium/canvaskit.js']) {
      File('${build.path}/canvaskit/$f')
        ..createSync(recursive: true)
        ..writeAsStringSync('rebuilt $f');
    }

    versionCanvasKit(build.path);

    final moved = '${build.path}/canvaskit/$_revision';
    expect(File('$moved/canvaskit.js').readAsStringSync(), 'rebuilt canvaskit.js');
    expect(File('$moved/chromium/canvaskit.js').readAsStringSync(),
        'rebuilt chromium/canvaskit.js');
    // The rebuilt chromium/ replaced the old one rather than merging into it.
    expect(File('$moved/chromium/canvaskit.wasm').existsSync(), isFalse);
    expect(
        Directory('${build.path}/canvaskit')
            .listSync()
            .map((e) => e.path.split(RegExp(r'[\\/]')).last),
        [_revision]);
  });

  // After a Flutter SDK upgrade the reused build/web still holds the previous
  // engine's canvaskit/<old revision>/, which Flutter never deletes
  // (review R5-1).
  test('a rebuild on a newer SDK drops the previous revision', () {
    final build = _build();
    const older = '0123456789abcdef0123456789abcdef01234567';
    File('${build.path}/canvaskit/$older/chromium/canvaskit.wasm')
      ..createSync(recursive: true)
      ..writeAsStringSync('old');

    versionCanvasKit(build.path);

    expect(
        Directory('${build.path}/canvaskit')
            .listSync()
            .map((e) => e.path.split(RegExp(r'[\\/]')).last),
        [_revision]);
    expect(Directory('${build.path}/canvaskit/$_revision/$older').existsSync(),
        isFalse);
  });

  test('a build without its own CanvasKit is refused', () {
    final build = _build();
    Directory('${build.path}/canvaskit').deleteSync(recursive: true);
    expect(() => versionCanvasKit(build.path), throwsStateError);
  });

  test('the bootstrap loads CanvasKit from the revision directory', () {
    final bootstrap = File('web/flutter_bootstrap.js').readAsStringSync();
    expect(bootstrap, contains('_flutter.buildConfig.useLocalCanvasKit'));
    expect(bootstrap, contains(
        "'canvaskit/' + _flutter.buildConfig.engineRevision + '/'"));
  });
}
