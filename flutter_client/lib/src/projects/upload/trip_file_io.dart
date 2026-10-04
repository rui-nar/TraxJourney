/// Native trip-file picking and upload (issue #469): the file is streamed
/// from disk into the request, never read whole. The web build uses
/// `trip_file_web.dart` instead, through `projects_notifier.dart`'s
/// conditional import.
library;

import 'package:file_picker/file_picker.dart';
import 'package:http/http.dart' as http;

import 'trip_file.dart';

/// Opens the system picker filtered on [extensions]; null when cancelled.
Future<TripFile?> pickTripFile(List<String> extensions) async {
  final picked = await FilePicker.pickFile(
    type: FileType.custom,
    allowedExtensions: extensions,
  );
  if (picked == null) return null;
  return StreamedTripFile(
      picked.name, await picked.length(), picked.readAsByteStream);
}

/// The uploader for this platform.
TripFileUploader platformTripFileUploader() => const HttpTripFileUploader();

/// Streams a [StreamedTripFile] through package:http with its length set, so
/// the request carries a Content-Length and no copy of the file.
class HttpTripFileUploader implements TripFileUploader {
  const HttpTripFileUploader();

  @override
  Future<TripFileUploadResponse> send({
    required Uri url,
    required Map<String, String> headers,
    required String field,
    required String filename,
    required TripFile file,
  }) async {
    final streamed = file as StreamedTripFile;
    final request = http.MultipartRequest('POST', url)
      ..headers.addAll(headers)
      ..files.add(http.MultipartFile(
          field, streamed.openRead(), streamed.size,
          filename: filename));
    final res = await http.Response.fromStream(await request.send());
    return (statusCode: res.statusCode, body: res.body);
  }
}
