/// Moves a web build's CanvasKit under its engine revision (#601 review R3-1).
///
/// Release builds take CanvasKit from the build (--no-web-resources-cdn)
/// rather than www.gstatic.com, whose URL carried the engine revision. The
/// server caches canvaskit/ for a day, so under a plain canvaskit/ path a
/// visitor returning after a release on a newer Flutter SDK could get the new
/// main.dart.js with yesterday's CanvasKit. This moves the build's canvaskit/
/// contents to `canvaskit/<engine revision>/`, which web/flutter_bootstrap.js
/// names, so each engine gets the CanvasKit it was built with.
///
///     dart run tool/version_canvaskit.dart build/web
///
/// Run it after every `flutter build web --no-web-resources-cdn`.
library;

import 'dart:io';

/// An engine revision: a git commit hash.
const _revision = '[0-9a-f]{7,64}';

/// A directory name that is an engine revision.
final _revisionShape = RegExp('^$_revision\$');

/// The engine revision `flutter build web` wrote into the build's bootstrap.
String engineRevisionOf(String webBuild) {
  final bootstrap = File('$webBuild/flutter_bootstrap.js').readAsStringSync();
  final revision =
      RegExp('"engineRevision":"($_revision)"').firstMatch(bootstrap);
  if (revision == null) {
    throw StateError('No engineRevision in $webBuild/flutter_bootstrap.js');
  }
  return revision[1]!;
}

/// Moves everything in `<webBuild>/canvaskit/` into
/// `<webBuild>/canvaskit/<revision>/`. A rerun on a moved build changes
/// nothing.
void versionCanvasKit(String webBuild) {
  final revision = engineRevisionOf(webBuild);
  final root = Directory('$webBuild/canvaskit');
  if (!root.existsSync()) {
    throw StateError('No canvaskit/ in $webBuild: was it built with '
        '--no-web-resources-cdn?');
  }
  // Listed before the target exists and matched by name: on Windows a listed
  // child's path is joined with '\', so it never equals one built with '/'
  // (review R4-1).
  final entries = <FileSystemEntity>[];
  for (final entry in root.listSync()) {
    final name = _name(entry);
    if (name == revision) continue;
    if (_revisionShape.hasMatch(name)) {
      // A previous SDK's moved CanvasKit, which Flutter never deletes from a
      // reused build/web: dropped, not nested inside the new one (review R5-1).
      entry.deleteSync(recursive: true);
      continue;
    }
    entries.add(entry);
  }
  final target = Directory('${root.path}/$revision')..createSync();
  for (final entry in entries) {
    final destination = '${target.path}/${_name(entry)}';
    // A rebuild into the same build/web brings canvaskit/chromium/ back next
    // to the previous run's <revision>/chromium/; the new copy replaces it
    // (review R4-2).
    switch (FileSystemEntity.typeSync(destination)) {
      case FileSystemEntityType.directory:
        Directory(destination).deleteSync(recursive: true);
      case FileSystemEntityType.notFound:
        break;
      default:
        File(destination).deleteSync();
    }
    entry.renameSync(destination);
  }
  if (!File('${target.path}/canvaskit.js').existsSync()) {
    throw StateError('${target.path} has no canvaskit.js');
  }
}

/// The last segment of [entry]'s path, whichever separator joined it.
String _name(FileSystemEntity entry) => entry.path.split(RegExp(r'[\\/]')).last;

void main(List<String> args) {
  if (args.length != 1) {
    stderr.writeln('usage: dart run tool/version_canvaskit.dart <web build>');
    exit(64);
  }
  versionCanvasKit(args.single);
  stdout.writeln('CanvasKit moved to ${args.single}/canvaskit/'
      '${engineRevisionOf(args.single)}/');
}
