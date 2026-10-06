part of 'project_facet.dart';

/// The elevation profile: the full track, the per-activity tracks and the
/// totals.
///
/// Empty for now: its fields move here from `ProjectNotifier` one facet at
/// a time (#294).
final class ElevationFacet extends ProjectFacet {
  ElevationFacet._();
}

/// Writes [ElevationFacet]. See `project_facet.dart` for who may hold one.
final class ElevationFacetWriter extends ProjectFacetWriter<ElevationFacet> {
  ElevationFacetWriter() : super(ElevationFacet._());

  @override
  void reset() {}
}
