/// Shows a trip video preview, an animated WebP
/// (docs/VIDEO_PREVIEW_PLAN.md, U4; review R1-4).
///
/// `Image.memory` animates WebP on Android and iOS, but on the web that
/// depends on the browser's image decoding, so the web build shows it in a
/// browser `<img>` instead, which every browser animates natively. The
/// conditional export keeps `dart:js_interop` / `package:web` out of the
/// mobile build.
library;

export 'video_preview_image_io.dart'
    if (dart.library.js_interop) 'video_preview_image_web.dart';
