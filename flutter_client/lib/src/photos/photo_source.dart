/// Photo picker + metadata extraction for the Polarsteps photo-upgrade
/// feature (issue #33).
///
/// Reuses the same [FilePicker] pattern already used for memory photo
/// uploads (`memory_dialog.dart`'s `_pickPhotos`) — `withData: true` gives
/// cross-platform (including web) raw bytes of the original file, so EXIF
/// survives intact. This file adds EXIF capture time/GPS extraction and a
/// perceptual hash on top of that, to feed [PhotoCandidate] for Phase 2's
/// matcher (`photo_match.dart`, not modified here).
library;

import 'dart:typed_data';

import 'package:exif/exif.dart';
import 'package:file_picker/file_picker.dart';

import '../core/picked_file_bytes.dart';
import 'perceptual_hash.dart';
import 'photo_match.dart';

/// EXIF capture timestamp and GPS location extracted from a photo's bytes,
/// or all-null fields when no EXIF data is present.
class ExifCaptureInfo {
  final DateTime? capturedAt;
  final double? lat;
  final double? lon;

  const ExifCaptureInfo({this.capturedAt, this.lat, this.lon});
}

/// A photo picked from the user's device, paired with the [PhotoCandidate]
/// derived from its EXIF metadata and pHash — ready to feed Phase 2's
/// matcher. [candidate] is null when the photo has no EXIF capture
/// timestamp: it can't be day-matched, so the review UI should surface it
/// as needing manual assignment rather than guessing a date.
class PickedPhoto {
  final Uint8List bytes;
  final String filename;
  final PhotoCandidate? candidate;

  const PickedPhoto({
    required this.bytes,
    required this.filename,
    this.candidate,
  });

  bool get needsManualAssignment => candidate == null;
}

/// Opens the system file picker (multi-select, images only) and returns a
/// [PickedPhoto] per selected file, with EXIF/pHash metadata already
/// extracted. Returns an empty list if the user cancels the picker.
Future<List<PickedPhoto>> pickPhotosForUpgrade() async {
  final files = await FilePicker.pickFiles(type: FileType.image);

  final photos = <PickedPhoto>[];
  for (final f in files) {
    final bytes = await f.readAsBytesOrNull();
    if (bytes == null) continue;
    photos.add(await buildPickedPhoto(bytes: bytes, filename: f.name));
  }
  return photos;
}

/// Opens the system file picker for a single image and returns the
/// resulting [PickedPhoto] with EXIF/pHash metadata already extracted, or
/// null if the user cancels the picker.
Future<PickedPhoto?> pickSinglePhotoForUpgrade() async {
  final file = await FilePicker.pickFile(type: FileType.image);
  if (file == null) return null;
  final bytes = await file.readAsBytesOrNull();
  if (bytes == null) return null;
  return buildPickedPhoto(bytes: bytes, filename: file.name);
}

/// Builds a [PickedPhoto] from already-read bytes: extracts EXIF and
/// computes the pHash. Pure computation on [bytes] — no file-picking I/O —
/// so it's the seam used by unit tests.
Future<PickedPhoto> buildPickedPhoto({
  required Uint8List bytes,
  required String filename,
}) async {
  final exifInfo = await extractExifCaptureInfo(bytes);
  final pHash = computeAverageHash(bytes);
  final capturedAt = exifInfo.capturedAt;

  final candidate = capturedAt == null
      ? null
      : PhotoCandidate(
          capturedAt: capturedAt,
          lat: exifInfo.lat,
          lon: exifInfo.lon,
          pHash: pHash,
        );

  return PickedPhoto(bytes: bytes, filename: filename, candidate: candidate);
}

/// Extracts capture timestamp and GPS coordinates from a photo's EXIF data.
/// Returns all-null fields (rather than throwing) when the bytes have no
/// EXIF block, aren't a supported format, or are corrupted — a missing
/// timestamp is a normal, expected case here, not an error.
Future<ExifCaptureInfo> extractExifCaptureInfo(Uint8List bytes) async {
  Map<String, IfdTag> data;
  try {
    data = await readExifFromBytes(bytes);
  } catch (_) {
    return const ExifCaptureInfo();
  }
  if (data.isEmpty) return const ExifCaptureInfo();

  final dateRaw = (data['EXIF DateTimeOriginal'] ?? data['Image DateTime'])
      ?.printable;
  final capturedAt = dateRaw == null ? null : _parseExifDateTime(dateRaw);

  final lat = _gpsCoordinate(
    data['GPS GPSLatitude']?.values,
    data['GPS GPSLatitudeRef']?.printable,
    negativeRef: 'S',
  );
  final lon = _gpsCoordinate(
    data['GPS GPSLongitude']?.values,
    data['GPS GPSLongitudeRef']?.printable,
    negativeRef: 'W',
  );

  return ExifCaptureInfo(capturedAt: capturedAt, lat: lat, lon: lon);
}

/// EXIF `DateTimeOriginal`/`DateTime` is formatted `"YYYY:MM:DD HH:MM:SS"`
/// with no timezone. The camera's wall-clock reading is taken as the trip's
/// local time and tagged UTC (rather than parsed as *this device's* local
/// time) so day-matching doesn't shift depending on what timezone the app
/// happens to be running in when the user reviews photos — callers should
/// pass `localOffset: Duration.zero` to [selectCandidatesForDay] to match.
DateTime? _parseExifDateTime(String raw) {
  final match =
      RegExp(r'^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})')
          .firstMatch(raw.trim());
  if (match == null) return null;
  try {
    return DateTime.utc(
      int.parse(match.group(1)!),
      int.parse(match.group(2)!),
      int.parse(match.group(3)!),
      int.parse(match.group(4)!),
      int.parse(match.group(5)!),
      int.parse(match.group(6)!),
    );
  } catch (_) {
    return null;
  }
}

double? _gpsCoordinate(
  IfdValues? values,
  String? ref, {
  required String negativeRef,
}) {
  if (values == null || values is! IfdRatios || ref == null) return null;
  double sum = 0.0;
  double unit = 1.0;
  for (final ratio in values.ratios) {
    sum += ratio.toDouble() * unit;
    unit /= 60.0;
  }
  return ref.toUpperCase() == negativeRef ? -sum : sum;
}
