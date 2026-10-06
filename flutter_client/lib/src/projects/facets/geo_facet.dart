part of 'project_facet.dart';

/// The trip's map geometry and the level of detail it holds.
///
/// Empty for now: its fields move here from `ProjectNotifier` one facet at
/// a time (#294).
final class GeoFacet extends ProjectFacet {
  GeoFacet._();
}

/// Writes [GeoFacet]. See `project_facet.dart` for who may hold one.
final class GeoFacetWriter extends ProjectFacetWriter<GeoFacet> {
  GeoFacetWriter() : super(GeoFacet._());

  @override
  void reset() {}
}
