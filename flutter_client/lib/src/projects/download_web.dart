import 'dart:js_interop';
import 'dart:typed_data' show Uint8List;

import 'package:web/web.dart' as web;

/// Triggers a browser file download for [bytes] as [filename] with the given
/// [mimeType] (blob URL + a click on a hidden anchor element).
void triggerBrowserDownload(Uint8List bytes, String mimeType, String filename) {
  final blob = web.Blob(
    [bytes.toJS as JSAny].toJS,
    web.BlobPropertyBag(type: mimeType),
  );
  final url = web.URL.createObjectURL(blob);
  (web.document.createElement('a') as web.HTMLAnchorElement)
    ..href = url
    ..download = filename
    ..click();
  web.URL.revokeObjectURL(url);
}
