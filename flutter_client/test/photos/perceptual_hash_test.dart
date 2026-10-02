import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;
import 'package:traxjourney_client/src/photos/perceptual_hash.dart';

/// An 8x8 PNG whose pixels spell out [hash]: bright where the bit is set,
/// dark where it is clear, in the hash's bit order (row-major, top-left is
/// bit 63). The hash grid is 8x8 too, so the expected hash is the pattern
/// itself.
Uint8List _pngSpelling(PerceptualHash hash) {
  final image = img.Image(width: 8, height: 8);
  for (var i = 0; i < 64; i++) {
    final word = i < 32 ? hash.high : hash.low;
    final bit = (word >>> (31 - i % 32)) & 1;
    final v = bit == 1 ? 230 : 20;
    image.setPixelRgb(i % 8, i ~/ 8, v, v, v);
  }
  return Uint8List.fromList(img.encodePng(image));
}

void main() {
  group('computeAverageHash', () {
    // The JS web build used to keep only the bottom 32 bits (issue #527).
    // These patterns differ from each other in both halves, so a hash that
    // drops a half fails here on whichever platform drops it.
    for (final pattern in const [
      PerceptualHash(0xA5C3F00F, 0x0FF05A3C),
      PerceptualHash(0xFFFFFFFF, 0x00000000),
      PerceptualHash(0x00000000, 0xFFFFFFFF),
      PerceptualHash(0x80000000, 0x00000001),
    ]) {
      test('reads back $pattern from an image that spells it', () {
        expect(computeAverageHash(_pngSpelling(pattern)), pattern);
      });
    }

    test('images that differ only in their top half get different hashes', () {
      const a = PerceptualHash(0xF0F0F0F0, 0x12345678);
      const b = PerceptualHash(0x0F0F0F0F, 0x12345678);
      final hashA = computeAverageHash(_pngSpelling(a))!;
      final hashB = computeAverageHash(_pngSpelling(b))!;
      expect(hashA.distanceTo(hashB), 32);
    });
  });

  group('PerceptualHash', () {
    test('equal halves are equal hashes', () {
      expect(const PerceptualHash(1, 2), const PerceptualHash(1, 2));
      expect(const PerceptualHash(1, 2).hashCode, const PerceptualHash(1, 2).hashCode);
      expect(const PerceptualHash(1, 2), isNot(const PerceptualHash(2, 1)));
    });

    test('distance counts bits in both halves', () {
      expect(const PerceptualHash(0x3, 0x1).distanceTo(const PerceptualHash(0, 0)), 3);
      expect(
        const PerceptualHash(0xFFFFFFFF, 0xFFFFFFFF).distanceTo(const PerceptualHash(0, 0)),
        64,
      );
    });
  });
}
