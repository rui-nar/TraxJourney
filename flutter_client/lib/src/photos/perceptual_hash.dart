/// The 64-bit average hash (aHash) used to tell whether two images show the
/// same photo (issue #33), kept free of Flutter imports so it compiles to any
/// target on its own.
///
/// The 64 bits are held as two unsigned 32-bit halves rather than one `int`:
/// on the JS web build an `int` is a double and bitwise operators work in 32
/// bits, so a 64-bit hash silently lost its top half there (issue #527).
/// 32-bit operations give the same result on the VM, JS and Wasm.
library;

import 'dart:typed_data';

import 'package:image/image.dart' as img;

/// A 64-bit perceptual hash. Bit 63 (the first pixel of the grid, top-left)
/// is the most significant bit of [high]; bit 0 (the last, bottom-right) is
/// the least significant bit of [low].
class PerceptualHash {
  /// Bits 63..32, as an unsigned 32-bit value.
  final int high;

  /// Bits 31..0, as an unsigned 32-bit value.
  final int low;

  const PerceptualHash(this.high, this.low)
      : assert(high >= 0 && high <= 0xFFFFFFFF),
        assert(low >= 0 && low <= 0xFFFFFFFF);

  /// Number of bits that differ from [other].
  int distanceTo(PerceptualHash other) =>
      _bitCount32(high ^ other.high) + _bitCount32(low ^ other.low);

  @override
  bool operator ==(Object other) =>
      other is PerceptualHash && other.high == high && other.low == low;

  @override
  int get hashCode => Object.hash(high, low);

  @override
  String toString() =>
      'PerceptualHash(0x${high.toRadixString(16).padLeft(8, '0')}'
      '${low.toRadixString(16).padLeft(8, '0')})';
}

int _bitCount32(int x) {
  var count = 0;
  for (var i = 0; i < 32; i++) {
    if ((x & 1) != 0) count++;
    x = x >>> 1;
  }
  return count;
}

/// Perceptual hash bit-grid size: 8x8 = 64 bits.
const _kHashSize = 8;

/// A simple, self-contained average hash (aHash): downscale to an
/// [_kHashSize]x[_kHashSize] grid, take each pixel's luminance, and set a
/// bit per pixel for whether it's above the grid's mean luminance. Chosen
/// over a fancier pHash (e.g. DCT-based) because aHash is a handful of
/// lines with no extra dependencies and is plenty robust to the
/// thumbnail-vs-original resolution/compression differences this feature
/// deals with. Returns null if [bytes] can't be decoded as an image — the
/// `image` package's format-sniffing can throw (rather than return null) on
/// very short/malformed input, so decode failures are caught here too.
PerceptualHash? computeAverageHash(Uint8List bytes) {
  img.Image? decoded;
  try {
    decoded = img.decodeImage(bytes);
  } catch (_) {
    return null;
  }
  if (decoded == null) return null;
  // decodeImage never applies the EXIF orientation tag to pixel data (that's
  // a separate, opt-in `bakeOrientation` transform) — without this, a
  // portrait phone photo stored as rotated sensor data hashes completely
  // differently from an already-upright thumbnail of the same shot.
  final oriented = img.bakeOrientation(decoded);
  final resized = img.copyResize(oriented, width: _kHashSize, height: _kHashSize);

  final luminances = <double>[];
  for (var y = 0; y < _kHashSize; y++) {
    for (var x = 0; x < _kHashSize; x++) {
      luminances.add(resized.getPixel(x, y).luminance.toDouble());
    }
  }
  final mean = luminances.reduce((a, b) => a + b) / luminances.length;

  // Each half takes exactly 32 shifts, so neither ever exceeds 32 bits.
  var high = 0;
  var low = 0;
  for (var i = 0; i < luminances.length; i++) {
    final bit = luminances[i] >= mean ? 1 : 0;
    if (i < 32) {
      high = (high << 1) | bit;
    } else {
      low = (low << 1) | bit;
    }
  }
  return PerceptualHash(high, low);
}
