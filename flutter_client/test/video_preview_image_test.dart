// The non-web VideoPreviewImage (docs/VIDEO_PREVIEW_PLAN.md, U4) shows the
// preview with Image.memory, which animates WebP on Android and iOS; the web
// one wraps a browser <img> and only builds for the web.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/video_preview_image.dart';

void main() {
  final png = base64Decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l'
      'EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=');

  Future<void> pump(WidgetTester tester, {bool dimmed = false}) =>
      tester.pumpWidget(MaterialApp(
        home: SizedBox(
          width: 320,
          height: 180,
          child: VideoPreviewImage(bytes: png, dimmed: dimmed),
        ),
      ));

  testWidgets('shows the bytes with Image.memory', (tester) async {
    await pump(tester);
    final image = tester.widget<Image>(find.byType(Image));
    expect(image.image, isA<MemoryImage>());
    expect((image.image as MemoryImage).bytes, png);
    expect(image.fit, BoxFit.contain);
    expect(tester.widget<Opacity>(find.byType(Opacity)).opacity, 1);
  });

  testWidgets('dimmed greys it out', (tester) async {
    await pump(tester, dimmed: true);
    expect(tester.widget<Opacity>(find.byType(Opacity)).opacity, lessThan(1));
  });
}
