/// A .gpx opened in or shared with the app from another app (issue #368):
/// Gmail, Files, a Chrome download, Komoot's "Open in…".
///
/// The file arrives outside any trip, so it is held here while the user signs
/// in (if needed) and picks the trip it belongs to on the trip picker
/// ([kIncomingGpxRoute]). That trip's screen then opens the usual
/// GpxImportDialog with the file already loaded: the dialog, its review step
/// and the server's GPX parse stay the only import path.
///
/// Two ways in:
/// - Android: MainActivity holds the opened or shared file. `take` on
///   [_channel] answers its name and bytes, read up to the cap and no
///   further, and MainActivity says `received` when one arrives while the
///   app runs.
/// - iOS (unverified until iOS ships): the system copies an opened document
///   into the app's Inbox, and Flutter hands its `file://` URL to the router
///   as a deep link. [IncomingFileRouteGuard] takes it first.
///
/// A shared file is untrusted input: any size, any type, any name. One larger
/// than the server's GPX cap is refused without being read; what is read is
/// judged by the server alone.
library;

import 'dart:io' show File, FileSystemException;
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

import '../core/project_ref.dart';

/// The server's GPX upload cap: `MAX_IMPORT_BYTES` in src/gpx/importer.py.
const int kGpxImportMaxBytes = 12 * 1024 * 1024;

/// The trip picker a received file leads to.
const String kIncomingGpxRoute = '/import-gpx';

const String kGpxTooLargeMessage =
    'This file is too large to import. The limit is 12 MB.';
const String kGpxUnreadableMessage = 'This file could not be read.';

/// A received file, read whole and within the cap.
class IncomingGpxFile {
  const IncomingGpxFile(this.name, this.bytes);

  final String name;
  final Uint8List bytes;
}

/// What came of reading a received file: the file, or why it was refused.
class IncomingFileRead {
  const IncomingFileRead.file(IncomingGpxFile this.file) : refusal = null;
  const IncomingFileRead.refused(String this.refusal) : file = null;

  final IncomingGpxFile? file;
  final String? refusal;
}

/// Reads a received file of [name], refusing it unread when [length] says it
/// is over [maxBytes], and the moment [open]'s stream passes [maxBytes] when
/// [length] was wrong. [open] is asked for at most `maxBytes + 1` bytes.
Future<IncomingFileRead> readIncomingFile({
  required String name,
  required Future<int> Function() length,
  required Stream<List<int>> Function(int end) open,
  int maxBytes = kGpxImportMaxBytes,
}) async {
  try {
    if (await length() > maxBytes) {
      return const IncomingFileRead.refused(kGpxTooLargeMessage);
    }
    final bytes = BytesBuilder(copy: false);
    await for (final chunk in open(maxBytes + 1)) {
      bytes.add(chunk);
      if (bytes.length > maxBytes) {
        return const IncomingFileRead.refused(kGpxTooLargeMessage);
      }
    }
    return IncomingFileRead.file(IncomingGpxFile(name, bytes.takeBytes()));
  } on FileSystemException {
    return const IncomingFileRead.refused(kGpxUnreadableMessage);
  }
}

/// Reads the local file a `file://` [uri] names (iOS's Inbox copy).
Future<IncomingFileRead> readFileUri(Uri uri) {
  final file = File(uri.toFilePath());
  return readIncomingFile(
    name: _nameOrDefault(uri.pathSegments.lastOrNull),
    length: file.length,
    open: (end) => file.openRead(0, end),
  );
}

/// Turns MainActivity's answer to `take` into a read.
@visibleForTesting
IncomingFileRead readFromPlatformAnswer(Map<Object?, Object?> answer) {
  if (answer['tooLarge'] == true) {
    return const IncomingFileRead.refused(kGpxTooLargeMessage);
  }
  final bytes = answer['bytes'];
  if (bytes is! Uint8List) {
    return const IncomingFileRead.refused(kGpxUnreadableMessage);
  }
  return IncomingFileRead.file(
      IncomingGpxFile(_nameOrDefault(answer['name'] as String?), bytes));
}

String _nameOrDefault(String? name) =>
    name == null || name.trim().isEmpty ? 'track.gpx' : name;

/// True for a location that is a file, not a route: what Android's
/// `content://` and iOS's `file://` hand-offs look like.
bool isIncomingFileLocation(String location) {
  final scheme = Uri.tryParse(location)?.scheme.toLowerCase();
  return scheme == 'content' || scheme == 'file';
}

/// Takes a file location the platform pushes as a route before go_router can
/// try to route it. It must be registered before the router's own route
/// information provider, which is why [IncomingGpx.start] runs before the
/// GoRouter is built: the first observer that answers true ends the dispatch.
class IncomingFileRouteGuard with WidgetsBindingObserver {
  IncomingFileRouteGuard(this.onFile);

  final void Function(Uri file) onFile;

  @override
  Future<bool> didPushRouteInformation(RouteInformation routeInformation) {
    final uri = routeInformation.uri;
    if (!isIncomingFileLocation(uri.toString())) {
      return SynchronousFuture(false);
    }
    // A content:// one is MainActivity's to read, and never meant to arrive.
    if (uri.scheme.toLowerCase() == 'file') onFile(uri);
    return SynchronousFuture(true);
  }
}

/// The received file, held from its arrival until a trip's screen takes it.
class IncomingGpx extends ChangeNotifier {
  static const _channel = MethodChannel('com.traxjourney.app/incoming_file');

  IncomingGpxFile? _file;
  String? _refusal;
  ProjectRef? _trip;
  void Function()? _onArrived;
  IncomingFileRouteGuard? _guard;

  /// The file waiting for a trip, if any.
  IncomingGpxFile? get file => _file;

  /// Why the last received file was refused, if it was.
  String? get refusal => _refusal;

  /// Starts receiving files on a native build; [onArrived] shows the trip
  /// picker. [launchLocation] is the platform's first route, which is a file
  /// when iOS launched the app to open one.
  void start({
    required void Function() onArrived,
    required String launchLocation,
  }) {
    _onArrived = onArrived;
    if (_guard != null) WidgetsBinding.instance.removeObserver(_guard!);
    _guard = IncomingFileRouteGuard((uri) => receive(readFileUri(uri)));
    WidgetsBinding.instance.addObserver(_guard!);

    final launch = Uri.tryParse(launchLocation);
    if (launch != null && launch.scheme.toLowerCase() == 'file') {
      receive(readFileUri(launch));
    }
    if (defaultTargetPlatform == TargetPlatform.android) {
      _channel.setMethodCallHandler((call) async {
        if (call.method == 'received') await _takeFromPlatform();
      });
      _takeFromPlatform();
    }
  }

  Future<void> _takeFromPlatform() async {
    final Map<Object?, Object?>? answer;
    try {
      answer = await _channel.invokeMapMethod<Object?, Object?>(
          'take', {'maxBytes': kGpxImportMaxBytes});
    } on MissingPluginException {
      return; // No MainActivity behind the channel: a test, or another embedder.
    } on PlatformException {
      return receive(Future.value(
          const IncomingFileRead.refused(kGpxUnreadableMessage)));
    }
    if (answer == null) return;
    await receive(Future.value(readFromPlatformAnswer(answer)));
  }

  /// Holds what [read] gives, in place of any earlier file, and shows the
  /// trip picker.
  Future<void> receive(Future<IncomingFileRead> read) async {
    final result = await read;
    _file = result.file;
    _refusal = result.refusal;
    _trip = null;
    notifyListeners();
    _onArrived?.call();
  }

  /// Records [trip] as the one the held file goes to.
  void chooseTrip(ProjectRef trip) => _trip = trip;

  /// The held file, once, for the trip it was chosen for; null for any other.
  IncomingGpxFile? takeFor(ProjectRef trip) {
    final file = _file;
    final chosen = _trip;
    if (file == null ||
        chosen == null ||
        chosen.name != trip.name ||
        chosen.ownerId != trip.ownerId) {
      return null;
    }
    _file = null;
    _trip = null;
    notifyListeners();
    return file;
  }

  /// Lets the held file, or the refusal, go.
  void dismiss() {
    _file = null;
    _refusal = null;
    _trip = null;
    notifyListeners();
  }
}

/// The app's one holder, like `api`: a file arrives before any screen exists.
final IncomingGpx incomingGpx = IncomingGpx();
