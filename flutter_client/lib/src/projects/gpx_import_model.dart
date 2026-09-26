/// What the server found in a picked GPX file (issue #260, unit 5).
///
/// Kept apart from the dialog so the parsing of the inspect response can be
/// tested without pumping a widget, and so the dialog holds a typed object
/// rather than a `Map<String, dynamic>` it indexes by string in twelve places.
library;

import '../map/geo_point.dart';
import '../map/polyline_decoder.dart';

/// One importable thing in the file — a recorded track, or a planned route.
class GpxCandidate {
  const GpxCandidate({
    required this.index,
    required this.pointCount,
    required this.distanceM,
    required this.isRoute,
    required this.hasTimes,
    required this.errors,
    this.warnings = const [],
    this.name,
    this.activityType,
    this.startedAt,
    this.endedAt,
    this.elapsedSeconds,
    this.movingSeconds,
    this.elevationGainM,
    this.outline = const [],
  });

  final int index;
  final String? name;

  /// One of the app's activity types, or null when the file's `<type>` was
  /// unrecognised — in which case the user picks rather than being handed a
  /// confident wrong answer.
  final String? activityType;

  final int pointCount;
  final double distanceM;

  /// True for a planned `<rte>`. It has no clock, so the date and times have to
  /// be asked for rather than prefilled.
  final bool isRoute;

  final bool hasTimes;
  final DateTime? startedAt;
  final DateTime? endedAt;
  final int? elapsedSeconds;
  final int? movingSeconds;

  /// Derived by the app from the file's elevations, never supplied by it, so
  /// every surface that shows it says so.
  final double? elevationGainM;

  /// Thinned outline for the preview thumbnail. Empty when the server sent
  /// none, which is the case for a candidate that cannot be imported anyway.
  final List<GeoPoint> outline;

  /// Why this one cannot be imported. Empty means it can.
  final List<String> errors;

  /// What the user should know before importing, such as a clock that looks
  /// wrong (issue #462). Never a reason not to import.
  final List<String> warnings;

  bool get isImportable => errors.isEmpty;

  static GpxCandidate fromJson(Map<String, dynamic> json) => GpxCandidate(
        index: (json['index'] as num).toInt(),
        name: json['name'] as String?,
        activityType: json['activity_type'] as String?,
        pointCount: (json['point_count'] as num?)?.toInt() ?? 0,
        distanceM: (json['distance_m'] as num?)?.toDouble() ?? 0,
        isRoute: json['is_route'] as bool? ?? false,
        hasTimes: json['has_times'] as bool? ?? false,
        startedAt: _parseTime(json['started_at']),
        endedAt: _parseTime(json['ended_at']),
        elapsedSeconds: (json['elapsed_seconds'] as num?)?.toInt(),
        movingSeconds: (json['moving_seconds'] as num?)?.toInt(),
        elevationGainM: (json['elevation_gain_m'] as num?)?.toDouble(),
        outline: _decodeOutline(json['polyline'] as String?),
        errors: ((json['errors'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(growable: false),
        warnings: ((json['warnings'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(growable: false),
      );

  static DateTime? _parseTime(Object? raw) {
    if (raw is! String || raw.isEmpty) return null;
    // Kept in UTC, deliberately. The server normalises a file's offset to
    // UTC on import and stores it that way, and the app has no per-activity
    // timezone yet (issue #365). Showing these in the device's local zone
    // would put 09:33 in the field for a 07:33Z ride, and the moment the
    // user touched any field that local wall time would be sent back as
    // though it were UTC — moving the activity by the offset. Display and
    // submission stay in one clock until there is a real timezone to hold.
    return DateTime.tryParse(raw)?.toUtc();
  }

  static List<GeoPoint> _decodeOutline(String? encoded) {
    if (encoded == null || encoded.isEmpty) return const [];
    try {
      return decodePolyline(encoded);
    } on Object {
      // A thumbnail is not worth failing an import over.
      return const [];
    }
  }
}

/// An activity in this trip that already holds the picked track.
class GpxDuplicate {
  const GpxDuplicate({required this.activityId, required this.name});

  final int activityId;
  final String name;

  static GpxDuplicate? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = (raw['activity_id'] as num?)?.toInt();
    if (id == null) return null;
    return GpxDuplicate(
        activityId: id, name: (raw['name'] as String?) ?? 'an activity');
  }
}

/// The whole of what inspecting a file told us.
class GpxInspection {
  const GpxInspection({
    required this.candidates,
    required this.errors,
    this.suggestedName,
    this.duplicateOf,
  });

  final List<GpxCandidate> candidates;
  final String? suggestedName;
  final GpxDuplicate? duplicateOf;

  /// Why the file as a whole is unusable. Empty means it is not.
  final List<String> errors;

  bool get needsAChoice => candidates.where((c) => c.isImportable).length > 1;

  static GpxInspection fromJson(Map<String, dynamic> json) => GpxInspection(
        candidates: ((json['candidates'] as List?) ?? const [])
            .map((c) => GpxCandidate.fromJson(c as Map<String, dynamic>))
            .toList(growable: false),
        suggestedName: json['suggested_name'] as String?,
        duplicateOf: GpxDuplicate.fromJson(json['duplicate_of']),
        errors: ((json['errors'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(growable: false),
      );
}
