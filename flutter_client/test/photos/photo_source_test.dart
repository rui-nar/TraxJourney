import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;
import 'package:traxjourney_client/src/photos/photo_match.dart';
import 'package:traxjourney_client/src/photos/photo_source.dart';

/// Builds a minimal, hand-rolled TIFF/EXIF byte buffer (the same container
/// format a JPEG's APP1 segment wraps) so tests can exercise
/// [extractExifCaptureInfo] against known tag values without needing a real
/// camera JPEG fixture. Layout: TIFF header -> IFD0 (ExifOffset [+
/// GPSInfo]) -> Exif sub-IFD (DateTimeOriginal) -> [GPS sub-IFD (lat/lon)].
/// All multi-byte fields are little-endian, per the TIFF spec.
Uint8List _buildExifBytes({
  required String dateTimeOriginal,
  double? lat,
  double? lon,
}) {
  final bytes = <int>[];

  void u16(int v) {
    bytes.add(v & 0xFF);
    bytes.add((v >> 8) & 0xFF);
  }

  void u32(int v) {
    bytes.add(v & 0xFF);
    bytes.add((v >> 8) & 0xFF);
    bytes.add((v >> 16) & 0xFF);
    bytes.add((v >> 24) & 0xFF);
  }

  final hasGps = lat != null && lon != null;
  final dtBytes = [...dateTimeOriginal.codeUnits, 0];

  // TIFF header.
  bytes.addAll('II'.codeUnits);
  u16(42);
  u32(8); // IFD0 offset

  // IFD0: ExifOffset, optionally GPSInfo.
  final ifd0EntryCount = hasGps ? 2 : 1;
  final ifd0Size = 2 + ifd0EntryCount * 12 + 4;
  const ifd0Start = 8;
  final exifIfdOffset = ifd0Start + ifd0Size;

  const exifIfdSize = 2 + 12 + 4; // 1 entry
  final exifStringOffset = exifIfdOffset + exifIfdSize;

  final gpsIfdOffset = exifStringOffset + dtBytes.length;
  const gpsIfdSize = 2 + 4 * 12 + 4; // 4 entries
  final gpsLatDataOffset = gpsIfdOffset + gpsIfdSize;
  final gpsLonDataOffset = gpsLatDataOffset + 24;

  u16(ifd0EntryCount);
  u16(0x8769); u16(4); u32(1); u32(exifIfdOffset); // ExifOffset (LONG)
  if (hasGps) {
    u16(0x8825); u16(4); u32(1); u32(gpsIfdOffset); // GPSInfo (LONG)
  }
  u32(0); // no next IFD

  // Exif sub-IFD.
  u16(1);
  u16(0x9003); u16(2); u32(dtBytes.length); u32(exifStringOffset); // DateTimeOriginal (ASCII)
  u32(0);
  bytes.addAll(dtBytes);

  if (hasGps) {
    void writeDms(double value) {
      final absVal = value.abs();
      final deg = absVal.floor();
      final minFull = (absVal - deg) * 60;
      final min = minFull.floor();
      final sec = ((minFull - min) * 60 * 1000).round();
      u32(deg); u32(1);
      u32(min); u32(1);
      u32(sec); u32(1000);
    }

    final latRef = lat >= 0 ? 'N' : 'S';
    final lonRef = lon >= 0 ? 'E' : 'W';

    // GPS sub-IFD.
    u16(4);
    u16(0x0001); u16(2); u32(2);
    bytes.addAll([latRef.codeUnitAt(0), 0, 0, 0]); // GPSLatitudeRef (inline ASCII)
    u16(0x0002); u16(5); u32(3); u32(gpsLatDataOffset); // GPSLatitude (RATIONAL x3)
    u16(0x0003); u16(2); u32(2);
    bytes.addAll([lonRef.codeUnitAt(0), 0, 0, 0]); // GPSLongitudeRef (inline ASCII)
    u16(0x0004); u16(5); u32(3); u32(gpsLonDataOffset); // GPSLongitude (RATIONAL x3)
    u32(0);

    writeDms(lat);
    writeDms(lon);
  }

  return Uint8List.fromList(bytes);
}

Uint8List _checkerboardPngBytes({int size = 32, int block = 4, bool invert = false}) {
  final image = img.Image(width: size, height: size);
  for (var y = 0; y < size; y++) {
    for (var x = 0; x < size; x++) {
      var on = ((x ~/ block) + (y ~/ block)).isEven;
      if (invert) on = !on;
      final v = on ? 255 : 0;
      image.setPixelRgb(x, y, v, v, v);
    }
  }
  return Uint8List.fromList(img.encodePng(image));
}

/// An image that's visibly different under rotation (bright top-left
/// quadrant, dark elsewhere) — unlike an evenly alternating checkerboard,
/// this can't coincidentally hash the same when rotated, so it actually
/// exercises orientation-correction rather than a rotation-symmetric image.
img.Image _asymmetricQuadrantImage({int size = 32}) {
  final image = img.Image(width: size, height: size);
  for (var y = 0; y < size; y++) {
    for (var x = 0; x < size; x++) {
      final v = (x < size / 2 && y < size / 2) ? 255 : 0;
      image.setPixelRgb(x, y, v, v, v);
    }
  }
  return image;
}

void main() {
  group('extractExifCaptureInfo', () {
    test('reads capture timestamp and GPS from EXIF data', () async {
      final bytes = _buildExifBytes(
        dateTimeOriginal: '2026:07:10 14:30:05',
        lat: 48.85,
        lon: 2.35,
      );
      final info = await extractExifCaptureInfo(bytes);

      expect(info.capturedAt, DateTime.utc(2026, 7, 10, 14, 30, 5));
      expect(info.lat, closeTo(48.85, 1e-3));
      expect(info.lon, closeTo(2.35, 1e-3));
    });

    test('negates coordinates for S/W refs', () async {
      final bytes = _buildExifBytes(
        dateTimeOriginal: '2026:01:02 03:04:05',
        lat: -10.5,
        lon: -20.25,
      );
      final info = await extractExifCaptureInfo(bytes);

      expect(info.lat, closeTo(-10.5, 1e-3));
      expect(info.lon, closeTo(-20.25, 1e-3));
    });

    test('a timestamp with no GPS block leaves lat/lon null', () async {
      final bytes = _buildExifBytes(dateTimeOriginal: '2026:03:04 05:06:07');
      final info = await extractExifCaptureInfo(bytes);

      expect(info.capturedAt, DateTime.utc(2026, 3, 4, 5, 6, 7));
      expect(info.lat, isNull);
      expect(info.lon, isNull);
    });

    test('bytes with no EXIF block return all-null fields, not a crash',
        () async {
      final info = await extractExifCaptureInfo(
          Uint8List.fromList([1, 2, 3, 4, 5, 6, 7, 8]));

      expect(info.capturedAt, isNull);
      expect(info.lat, isNull);
      expect(info.lon, isNull);
    });
  });

  group('computeAverageHash', () {
    test('near-identical images hash to a small Hamming distance', () {
      final a = _checkerboardPngBytes();
      // One pixel different from `a` — still overwhelmingly the same image.
      final image = img.decodeImage(a)!;
      image.setPixelRgb(0, 0, 128, 128, 128);
      final b = Uint8List.fromList(img.encodePng(image));

      final hashA = computeAverageHash(a);
      final hashB = computeAverageHash(b);

      expect(hashA, isNotNull);
      expect(hashB, isNotNull);
      expect(hammingDistance(hashA!, hashB!), lessThan(4));
    });

    test('very different images hash to a large Hamming distance', () {
      final checkerboard = computeAverageHash(_checkerboardPngBytes());
      final inverted =
          computeAverageHash(_checkerboardPngBytes(invert: true));

      expect(checkerboard, isNotNull);
      expect(inverted, isNotNull);
      expect(hammingDistance(checkerboard!, inverted!), greaterThan(32));
    });

    test('undecodable bytes return null instead of throwing', () {
      expect(computeAverageHash(Uint8List.fromList([9, 9, 9])), isNull);
    });

    test('bakes EXIF orientation before hashing, so a sensor-rotated photo '
        'still matches its upright reference', () {
      final upright = _asymmetricQuadrantImage();
      final uprightHash =
          computeAverageHash(Uint8List.fromList(img.encodePng(upright)));

      // Simulate a portrait photo stored as rotated sensor data (as real
      // camera JPEGs do) with orientation 6 recording that a 90° CW rotation
      // is needed to reach the upright display orientation.
      final sensorRotated = img.copyRotate(upright, angle: -90);
      sensorRotated.exif.imageIfd.orientation = 6;
      final rotatedBytes = Uint8List.fromList(img.encodeJpg(sensorRotated));
      final rotatedHash = computeAverageHash(rotatedBytes);

      expect(uprightHash, isNotNull);
      expect(rotatedHash, isNotNull);
      expect(hammingDistance(uprightHash!, rotatedHash!), lessThan(4));
    });
  });

  group('buildPickedPhoto', () {
    test('a photo with no EXIF timestamp needs manual assignment', () async {
      final photo = await buildPickedPhoto(
        bytes: Uint8List.fromList([1, 2, 3]),
        filename: 'no-exif.jpg',
      );

      expect(photo.candidate, isNull);
      expect(photo.needsManualAssignment, isTrue);
    });

    test('a photo with an EXIF timestamp produces a matchable candidate',
        () async {
      final exifBytes = _buildExifBytes(
        dateTimeOriginal: '2026:07:10 14:30:00',
        lat: 48.85,
        lon: 2.35,
      );
      final photo =
          await buildPickedPhoto(bytes: exifBytes, filename: 'trip.jpg');

      expect(photo.needsManualAssignment, isFalse);
      expect(photo.candidate!.capturedAt, DateTime.utc(2026, 7, 10, 14, 30, 0));
      expect(photo.candidate!.lat, closeTo(48.85, 1e-3));
      expect(photo.candidate!.lon, closeTo(2.35, 1e-3));
    });
  });
}
