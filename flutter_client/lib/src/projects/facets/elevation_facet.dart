part of 'project_facet.dart';

/// The elevation profile: the full track, the per-activity tracks and the
/// totals.
final class ElevationFacet extends ProjectFacet {
  ElevationFacet._();

  /// Full distance-indexed track for all activities — used by the map panel
  /// to map a tapped GeoPoint back to a distance on the elevation chart.
  List<(double, GeoPoint)> _fullTrack = const [];
  List<(double, GeoPoint)> get fullTrack => _fullTrack;

  /// Per-activity distance-indexed tracks (0-based distances) — used by
  /// ElevationChart to map chart x-position to a map position.
  /// Keys are activity_id as String.
  Map<String, List<(double, GeoPoint)>> _perActivityTracks = const {};
  Map<String, List<(double, GeoPoint)>> get perActivityTracks =>
      _perActivityTracks;

  // Cached aggregate stats — computed once in load(), not on every build.
  double _totalDistanceM = 0;
  int _totalMovingSeconds = 0;
  double _totalElevationGainM = 0;
  double get totalDistanceM => _totalDistanceM;
  int get totalMovingSeconds => _totalMovingSeconds;
  double get totalElevationGainM => _totalElevationGainM;

  void _reset() {
    _fullTrack = const [];
    _perActivityTracks = const {};
    _totalDistanceM = 0;
    _totalMovingSeconds = 0;
    _totalElevationGainM = 0;
    _markChanged();
  }
}

/// Writes [ElevationFacet]. See `project_facet.dart` for who may hold one.
final class ElevationFacetWriter extends ProjectFacetWriter<ElevationFacet> {
  ElevationFacetWriter() : super(ElevationFacet._());

  /// Replaces both tracks, as one change.
  void setTracks(List<(double, GeoPoint)> fullTrack,
      Map<String, List<(double, GeoPoint)>> perActivityTracks) {
    facet._fullTrack = fullTrack;
    facet._perActivityTracks = perActivityTracks;
    markChanged();
  }

  /// Replaces the cached aggregate stats, as one change.
  void setTotals({
    required double distanceM,
    required int movingSeconds,
    required double elevationGainM,
  }) {
    facet._totalDistanceM = distanceM;
    facet._totalMovingSeconds = movingSeconds;
    facet._totalElevationGainM = elevationGainM;
    markChanged();
  }

  @override
  void reset() => facet._reset();
}
