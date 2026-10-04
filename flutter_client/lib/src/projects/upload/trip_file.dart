/// A picked trip file and the transport that uploads it (issue #469).
///
/// A trip archive can be 1 GB, so the picked file is held as a handle (its
/// name, its size and a way to read it), never as bytes. Each platform's
/// uploader streams it: native from a fresh read stream, web by handing the
/// browser the file itself (`trip_file_io.dart`, `trip_file_web.dart`).
library;

/// A picked trip file. Platform implementations add the way to read it.
abstract interface class TripFile {
  /// The file's name as picked, extension included.
  String get name;

  /// The file's size in bytes, known without reading it.
  int get size;
}

/// A [TripFile] read through a stream: the native picker's files, and tests'.
class StreamedTripFile implements TripFile {
  StreamedTripFile(this.name, this.size, this._open);

  @override
  final String name;

  @override
  final int size;

  final Stream<List<int>> Function() _open;

  /// A new stream over the whole file. Every upload, a retry included, opens
  /// its own: a stream can only be listened to once.
  Stream<List<int>> openRead() => _open();
}

/// The server's answer to an upload: its status and its body as text.
typedef TripFileUploadResponse = ({int statusCode, String body});

/// Sends a [TripFile] as the one file part of a multipart POST.
abstract interface class TripFileUploader {
  /// POSTs [file] to [url] as part [field] named [filename], with [headers].
  /// Any status is answered; only a failure to reach the server throws.
  Future<TripFileUploadResponse> send({
    required Uri url,
    required Map<String, String> headers,
    required String field,
    required String filename,
    required TripFile file,
  });
}
