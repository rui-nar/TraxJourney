part of 'project_facet.dart';

/// What the user has selected: the selected item and day, the filters and
/// whether journals show.
///
/// Empty for now: its fields move here from `ProjectNotifier` one facet at
/// a time (#294).
final class SelectionFacet extends ProjectFacet {
  SelectionFacet._();
}

/// Writes [SelectionFacet]. See `project_facet.dart` for who may hold one.
final class SelectionFacetWriter extends ProjectFacetWriter<SelectionFacet> {
  SelectionFacetWriter() : super(SelectionFacet._());

  @override
  void reset() {}
}
