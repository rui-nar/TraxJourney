/// Web trip-file picking and upload (issue #469): the browser is handed the
/// picked `File` itself, inside a `FormData` sent with `fetch`, and streams it
/// from disk. The file's bytes never enter the Dart heap.
///
/// file_picker is not used here: its web files expose only bytes, Dart
/// streams and a `blob:` URL, never the `File`, and package:http's
/// BrowserClient buffers the whole body before sending it.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

import 'trip_file.dart';

/// A [TripFile] that is the browser's own picked file.
class BrowserTripFile implements TripFile {
  BrowserTripFile(this.file);

  final web.File file;

  @override
  String get name => file.name;

  @override
  int get size => file.size;
}

/// Opens the browser's file chooser filtered on [extensions]; null when
/// cancelled. Called straight from a tap, before any await, so the browser
/// counts the click as a user gesture.
Future<TripFile?> pickTripFile(List<String> extensions) {
  final done = Completer<TripFile?>();
  final input = web.HTMLInputElement()
    ..type = 'file'
    ..accept = extensions.map((e) => '.$e').join(',')
    ..style.display = 'none';
  void finish(web.Event _) {
    final file = input.files?.item(0);
    input.remove();
    if (!done.isCompleted) {
      done.complete(file == null ? null : BrowserTripFile(file));
    }
  }

  input
    ..addEventListener('change', finish.toJS)
    ..addEventListener('cancel', finish.toJS);
  web.document.body!.appendChild(input);
  input.click();
  return done.future;
}

/// The uploader for this platform.
TripFileUploader platformTripFileUploader() => const FetchTripFileUploader();

/// Sends a [BrowserTripFile] with `fetch` and a `FormData` body, which the
/// browser streams from the file.
class FetchTripFileUploader implements TripFileUploader {
  const FetchTripFileUploader();

  @override
  Future<TripFileUploadResponse> send({
    required Uri url,
    required Map<String, String> headers,
    required String field,
    required String filename,
    required TripFile file,
  }) async {
    final form = web.FormData()
      ..append(field, (file as BrowserTripFile).file, filename);
    final requestHeaders = web.Headers();
    headers.forEach((name, value) => requestHeaders.append(name, value));
    try {
      final res = await web.window
          .fetch(
            url.toString().toJS,
            web.RequestInit(
                method: 'POST', headers: requestHeaders, body: form),
          )
          .toDart;
      final body = (await res.text().toDart).toDart;
      return (statusCode: res.status, body: body);
    } catch (e) {
      // A rejected fetch is a JS error, not an Exception: give the caller
      // the same ClientException package:http's BrowserClient threw.
      throw http.ClientException('$e', url);
    }
  }
}
