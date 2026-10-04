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
    this.activityTypeExact,
    this.isConnection = false,
    this.startedAt,
    this.endedAt,
    this.timezone,
    this.startLocal,
    this.endLocal,
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

  /// The precise type the file named (a kayak is "Kayaking", where
  /// [activityType] says "Workout"). Null from a server that predates it, in
  /// which case [activityType] is all there is.
  final String? activityTypeExact;

  /// What the review step shows and sends: the precise type when there is one.
  String? get suggestedType => activityTypeExact ?? activityType;

  /// True for a connecting segment an export wrote between two activities. It
  /// is listed, but is not an activity and is never part of "Import all".
  final bool isConnection;

  final int pointCount;
  final double distanceM;

  /// True for a planned `<rte>`. It has no clock, so the date and times have to
  /// be asked for rather than prefilled.
  final bool isRoute;

  final bool hasTimes;
  final DateTime? startedAt;
  final DateTime? endedAt;

  /// IANA zone at the track's first point (issue #365). Null from a server
  /// that predates it.
  final String? timezone;

  /// [startedAt]/[endedAt] as the wall clock in [timezone]. Naive: the fields
  /// of the [DateTime] are the clock face, and the device's zone has no part
  /// in them. Null when the server gave none, and then the UTC instants above
  /// are all there is to show.
  final DateTime? startLocal;
  final DateTime? endLocal;
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
        activityTypeExact: json['activity_type_exact'] as String?,
        isConnection: json['is_connection'] as bool? ?? false,
        pointCount: (json['point_count'] as num?)?.toInt() ?? 0,
        distanceM: (json['distance_m'] as num?)?.toDouble() ?? 0,
        isRoute: json['is_route'] as bool? ?? false,
        hasTimes: json['has_times'] as bool? ?? false,
        startedAt: _parseTime(json['started_at']),
        endedAt: _parseTime(json['ended_at']),
        timezone: json['timezone'] as String?,
        startLocal: _parseLocal(json['start_local']),
        endLocal: _parseLocal(json['end_local']),
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
    // Kept in UTC, deliberately: these are instants, and showing them in the
    // device's zone would put 09:33 in the field for a 07:33Z ride. The
    // wall clock where the track was recorded comes from the server as
    // `start_local`/`end_local` (see [_parseLocal]); this is what a server
    // that sends none falls back to.
    return DateTime.tryParse(raw)?.toUtc();
  }

  /// A naive wall clock, read field by field. Any offset the string carries is
  /// ignored rather than converted, so the device's zone never shifts it.
  static DateTime? _parseLocal(Object? raw) {
    if (raw is! String || raw.isEmpty) return null;
    final m = RegExp(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?')
        .firstMatch(raw);
    if (m == null) return null;
    int g(int i) => int.parse(m.group(i) ?? '0');
    return DateTime.utc(g(1), g(2), g(3), g(4), g(5), g(6));
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

  /// What "Import all" would import: every importable track that is not a
  /// connecting segment.
  List<GpxCandidate> get importAllCandidates => candidates
      .where((c) => c.isImportable && !c.isConnection)
      .toList(growable: false);

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
