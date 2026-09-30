/// Android, iOS and desktop [VideoPreviewImage]: `Image.memory` animates
/// WebP there. See `video_preview_image.dart`.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

class VideoPreviewImage extends StatelessWidget {
  /// The preview's animated WebP.
  final Uint8List bytes;

  /// Greyed out, for a preview of other settings than the current ones.
  final bool dimmed;

  const VideoPreviewImage({super.key, required this.bytes, this.dimmed = false});

  @override
  Widget build(BuildContext context) => Opacity(
        opacity: dimmed ? 0.4 : 1,
        child: Image.memory(
          bytes,
          fit: BoxFit.contain,
          gaplessPlayback: true,
          errorBuilder: (context, error, stack) => Center(
            child: Icon(Icons.broken_image_outlined,
                color: Theme.of(context).colorScheme.outline),
          ),
        ),
      );
}
