/// Web [VideoPreviewImage]: a browser `<img>` whose `src` is a Blob URL of
/// the WebP, so every browser animates it natively (review R1-4). The URL is
/// revoked when the bytes change and on dispose. See
/// `video_preview_image.dart`.
library;

import 'dart:js_interop';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

class VideoPreviewImage extends StatefulWidget {
  /// The preview's animated WebP.
  final Uint8List bytes;

  /// Greyed out, for a preview of other settings than the current ones.
  final bool dimmed;

  const VideoPreviewImage({super.key, required this.bytes, this.dimmed = false});

  @override
  State<VideoPreviewImage> createState() => _VideoPreviewImageState();
}

class _VideoPreviewImageState extends State<VideoPreviewImage> {
  late String _url = _blobUrl(widget.bytes);
  web.HTMLImageElement? _img;

  static String _blobUrl(Uint8List bytes) => web.URL.createObjectURL(web.Blob(
        [bytes.toJS as JSAny].toJS,
        web.BlobPropertyBag(type: 'image/webp'),
      ));

  // Opacity over a platform view isn't applied on every web renderer, so
  // the element is dimmed with CSS instead.
  String get _opacity => widget.dimmed ? '0.4' : '1';

  @override
  void didUpdateWidget(VideoPreviewImage old) {
    super.didUpdateWidget(old);
    if (!identical(old.bytes, widget.bytes)) {
      final previous = _url;
      _url = _blobUrl(widget.bytes);
      _img?.src = _url;
      web.URL.revokeObjectURL(previous);
    }
    _img?.style.opacity = _opacity;
  }

  @override
  void dispose() {
    web.URL.revokeObjectURL(_url);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => HtmlElementView.fromTagName(
        tagName: 'img',
        onElementCreated: (element) {
          final img = element as web.HTMLImageElement;
          img.src = _url;
          img.alt = 'Video preview';
          img.style
            ..width = '100%'
            ..height = '100%'
            ..objectFit = 'contain'
            ..opacity = _opacity;
          _img = img;
        },
      );
}
