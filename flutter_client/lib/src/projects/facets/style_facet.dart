part of 'project_facet.dart';

/// How the trip is drawn: track colours, width and alternation, colour-by-type,
/// the type styles, the elevation chart's colour and line, and the languages.
final class StyleFacet extends ProjectFacet {
  StyleFacet._();

  // Named so [_reset] puts back the same defaults a fresh facet starts with.
  static const _defaultTrackColor = Color(0xFF6B7280); // gray-500 — shown while project loads
  static const _defaultTrackWidth = 2.5;

  Color _trackColor = _defaultTrackColor;
  Color? _trackSecondaryColor;
  double _trackWidth = _defaultTrackWidth;
  bool _alternatingTrackColors = false;
  Color? _elevationChartColor;
  bool _elevationChartShowLine = true;
  bool _colorByType = false;
  Map<String, Map<String, dynamic>> _typeStyles = {};
  List<String> _languages = [];

  Color get trackColor => _trackColor;

  /// null = auto-derive from primary
  Color? get trackSecondaryColor => _trackSecondaryColor;

  double get trackWidth => _trackWidth;

  bool get alternatingTrackColors => _alternatingTrackColors;

  /// null = "auto" → match the map track line (#22)
  Color? get elevationChartColor => _elevationChartColor;

  bool get elevationChartShowLine => _elevationChartShowLine;

  /// Opt-in per-type colouring (issue #95). Off by default so existing
  /// projects keep today's flat trackColor line rendering unchanged.
  bool get colorByType => _colorByType;

  /// Per-bucket overrides, keyed by activity bucket ("ride"/"run"/"hike"/
  /// "other") or segment type ("flight"/"train"/"bus"/"boat"). Each value
  /// e.g. {"color": "#RRGGBB", "style": "solid"|"dashed"|"dotted"}. Missing
  /// bucket = built-in default (see design_tokens.dart resolveTypeStyle).
  Map<String, Map<String, dynamic>> get typeStyles => _typeStyles;

  /// Translation languages.
  List<String> get languages => _languages;

  /// Colour the elevation chart actually renders with: the user's explicit
  /// override, or — when unset ("auto") — the map track line colour, so the
  /// chart matches the line on the map by default (issue #22).
  Color get effectiveElevationChartColor => _elevationChartColor ?? _trackColor;

  static Color? _hexColor(String? hex) =>
      (hex != null && hex.length == 7 && hex.startsWith('#'))
          ? Color(int.parse(hex.substring(1), radix: 16) | 0xFF000000)
          : null;

  void _setTrackStyle({
    Color? color,
    required bool setSecondary,
    Color? secondaryColor,
    double? width,
    bool? alternating,
    required bool setElevationColor,
    Color? elevationColor,
    bool? elevationShowLine,
    bool? colorByTypeEnabled,
    Map<String, Map<String, dynamic>>? typeStyleOverrides,
  }) {
    if (color != null) _trackColor = color;
    if (setSecondary) _trackSecondaryColor = secondaryColor;
    if (width != null) _trackWidth = width;
    if (alternating != null) _alternatingTrackColors = alternating;
    if (setElevationColor) _elevationChartColor = elevationColor;
    if (elevationShowLine != null) _elevationChartShowLine = elevationShowLine;
    if (colorByTypeEnabled != null) _colorByType = colorByTypeEnabled;
    if (typeStyleOverrides != null) _typeStyles = typeStyleOverrides;
    _markChanged();
  }

  // The style block of a project-details response, read the same way by
  // `load()` and by the background refresh after edits (issue #572).
  void _applyDetails(Map<String, dynamic> details) {
    final color = _hexColor(details['track_color'] as String?);
    if (color != null) _trackColor = color;
    _trackSecondaryColor = _hexColor(details['track_secondary_color'] as String?);
    final rawWidth = details['track_width'] as num?;
    if (rawWidth != null) _trackWidth = rawWidth.toDouble();
    final rawAlt = details['alternating_track_colors'] as bool?;
    if (rawAlt != null) _alternatingTrackColors = rawAlt;
    _elevationChartColor = _hexColor(details['elevation_chart_color'] as String?);
    final rawElLine = details['elevation_chart_show_line'] as bool?;
    if (rawElLine != null) _elevationChartShowLine = rawElLine;
    final rawLangs = details['languages'];
    if (rawLangs is List) _languages = rawLangs.cast<String>();
    final rawColorByType = details['color_by_type'] as bool?;
    if (rawColorByType != null) _colorByType = rawColorByType;
    final rawTypeStyles = details['type_styles'];
    _typeStyles = rawTypeStyles is Map
        ? rawTypeStyles.map((k, v) =>
            MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
        : {};
    _markChanged();
  }

  void _reset() {
    _trackColor = _defaultTrackColor;
    _trackSecondaryColor = null;
    _trackWidth = _defaultTrackWidth;
    _alternatingTrackColors = false;
    _elevationChartColor = null;
    _elevationChartShowLine = true;
    _colorByType = false;
    _typeStyles = {};
    _languages = [];
    _markChanged();
  }
}

/// Writes [StyleFacet]. See `project_facet.dart` for who may hold one.
final class StyleFacetWriter extends ProjectFacetWriter<StyleFacet> {
  StyleFacetWriter() : super(StyleFacet._());

  /// Passed as [setTrackStyle]'s `secondaryColor` or `elevationColor` to leave
  /// that colour as it is; null clears it.
  static const Object unset = Object();

  /// Applies the style fields that are given, as one change.
  void setTrackStyle({
    Color? color,
    Object? secondaryColor = unset,
    double? width,
    bool? alternating,
    Object? elevationColor = unset,
    bool? elevationShowLine,
    bool? colorByTypeEnabled,
    Map<String, Map<String, dynamic>>? typeStyleOverrides,
  }) {
    facet._setTrackStyle(
      color: color,
      setSecondary: secondaryColor != unset,
      secondaryColor: secondaryColor == unset ? null : secondaryColor as Color?,
      width: width,
      alternating: alternating,
      setElevationColor: elevationColor != unset,
      elevationColor: elevationColor == unset ? null : elevationColor as Color?,
      elevationShowLine: elevationShowLine,
      colorByTypeEnabled: colorByTypeEnabled,
      typeStyleOverrides: typeStyleOverrides,
    );
  }

  /// Replaces the translation languages with a copy of [langs].
  void setLanguages(List<String> langs) {
    facet._languages = List<String>.from(langs);
    markChanged();
  }

  /// Reads the style block of a project-details response.
  void applyDetails(Map<String, dynamic> details) =>
      facet._applyDetails(details);

  // Single-field writes, for tests.
  void setTrackColor(Color v) {
    facet._trackColor = v;
    markChanged();
  }

  void setElevationChartColor(Color? v) {
    facet._elevationChartColor = v;
    markChanged();
  }

  @override
  void reset() => facet._reset();
}
