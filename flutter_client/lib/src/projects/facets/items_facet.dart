part of 'project_facet.dart';

/// The trip's content: activities, items, people, groups, day-meta, trip dates,
/// sleeping options and counters.
///
/// Empty for now: its fields move here from `ProjectNotifier` one facet at
/// a time (#294).
final class ItemsFacet extends ProjectFacet {
  ItemsFacet._();
}

/// Writes [ItemsFacet]. See `project_facet.dart` for who may hold one.
final class ItemsFacetWriter extends ProjectFacetWriter<ItemsFacet> {
  ItemsFacetWriter() : super(ItemsFacet._());

  @override
  void reset() {}
}
