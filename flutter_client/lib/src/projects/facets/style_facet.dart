part of 'project_facet.dart';

/// How the trip is drawn: track colours, width and alternation, colour-by-type,
/// the type styles, the elevation chart's colour and line, and the languages.
///
/// Empty for now: its fields move here from `ProjectNotifier` one facet at
/// a time (#294).
final class StyleFacet extends ProjectFacet {
  StyleFacet._();
}

/// Writes [StyleFacet]. See `project_facet.dart` for who may hold one.
final class StyleFacetWriter extends ProjectFacetWriter<StyleFacet> {
  StyleFacetWriter() : super(StyleFacet._());

  @override
  void reset() {}
}
