library;

import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

// ── Accent (crimson red) ─────────────────────────────────────────────────────
const kAccent     = Color(0xFFDC2626);
const kAccentDark = Color(0xFFEF4444);
const kAccentSoft = Color(0x1ADC2626);

// ── Strava brand ─────────────────────────────────────────────────────────────
const kStrava = Color(0xFFFC4C02);

// ── Social share-target brands (Share memory modal) ──────────────────────────
const kWhatsApp     = Color(0xFF25D366);
const kFacebookBlue = Color(0xFF1877F2);

// ── Semantic ─────────────────────────────────────────────────────────────────
const kSuccess     = Color(0xFF16A34A);
const kSuccessDark = Color(0xFF22C55E);
const kWarning     = Color(0xFFD97706);
const kWarningDark = Color(0xFFF59E0B);

// ── Activity type colours (map polylines + icon boxes) ────────────────────────
const kColorRide  = Color(0xFF4FC3F7);
const kColorRun   = Color(0xFFEF5350);
const kColorHike  = Color(0xFF66BB6A);
const kColorOther = Color(0xFFAB47BC);
const kColorAlt   = Color(0xFFFFA726);

// ── Segment type colours (connecting flight/train/bus/boat legs) ─────────────
const kColorFlight = Color(0xFF42A5F5);
const kColorTrain  = Color(0xFF8D6E63);
const kColorBus    = Color(0xFFFFB300);
const kColorBoat   = Color(0xFF26C6DA);

/// Line-rendering style for a per-type override (issue #95).
enum LineStyleKind { solid, dashed, dotted }

LineStyleKind lineStyleFromName(String? name) => switch (name) {
      'dashed' => LineStyleKind.dashed,
      'dotted' => LineStyleKind.dotted,
      _        => LineStyleKind.solid,
    };

String lineStyleName(LineStyleKind style) => switch (style) {
      LineStyleKind.solid  => 'solid',
      LineStyleKind.dashed => 'dashed',
      LineStyleKind.dotted => 'dotted',
    };

/// Bucket key for an activity's raw Strava `sport_type` — collapses the many
/// Strava sub-types into the same 4 groups already used for the panel's
/// activity icon colours.
String activityTypeBucket(String? sportType) => switch (sportType?.toLowerCase()) {
      'ride' || 'virtualride' || 'ebikeride' => 'ride',
      'run' || 'virtualrun'                  => 'run',
      'hike' || 'walk'                       => 'hike',
      _                                      => 'other',
    };

/// Bucket key for a connecting segment's `segment_type`. Unrecognised/null
/// types fall back to 'other_segment' (matches [_iconForSegmentType]'s
/// generic route icon).
String segmentTypeBucket(String? segmentType) => switch (segmentType?.toLowerCase()) {
      'flight' => 'flight',
      'train'  => 'train',
      'bus'    => 'bus',
      'boat'   => 'boat',
      _        => 'other_segment',
    };

/// Built-in default colour for a bucket key, used both as the panel's
/// always-on icon colour and as the map's per-type colour once a project
/// opts into `color_by_type` (issue #95).
Color defaultTypeColor(String bucket) => switch (bucket) {
      'ride'  => kColorRide,
      'run'   => kColorRun,
      'hike'  => kColorHike,
      'flight' => kColorFlight,
      'train'  => kColorTrain,
      'bus'    => kColorBus,
      'boat'   => kColorBoat,
      _        => kColorAlt,
    };

/// Built-in default line style for a bucket — solid for activities, dashed
/// for segments, matching today's (pre-issue-#95) map rendering.
LineStyleKind defaultTypeLineStyle(String bucket, {required bool isSegment}) =>
    isSegment ? LineStyleKind.dashed : LineStyleKind.solid;

/// Resolves the effective (colour, line style) for [bucket], applying a
/// project's `type_styles` override (if any) over the built-in default.
/// [overrides] is the raw per-bucket map decoded from the API, e.g.
/// `{"color": "#RRGGBB", "style": "solid"|"dashed"|"dotted"}`.
({Color color, LineStyleKind style}) resolveTypeStyle(
  String bucket, {
  required bool isSegment,
  Map<String, dynamic>? overrides,
}) {
  final rawColor = overrides?['color'] as String?;
  final color = (rawColor != null && rawColor.length == 7 && rawColor.startsWith('#'))
      ? Color(int.parse(rawColor.substring(1), radix: 16) | 0xFF000000)
      : defaultTypeColor(bucket);
  final rawStyle = overrides?['style'] as String?;
  final style = rawStyle != null
      ? lineStyleFromName(rawStyle)
      : defaultTypeLineStyle(bucket, isSegment: isSegment);
  return (color: color, style: style);
}

Color iconBoxBg(Color c) => c.withValues(alpha: 0.15);

Color iconBoxFg(Color c, {bool dark = false}) => dark
    ? c
    : HSLColor.fromColor(c)
        .withLightness(
            (HSLColor.fromColor(c).lightness * 0.65).clamp(0.0, 1.0))
        .toColor();

// ── Sleeping-category dots (Edit Day modal) ──────────────────────────────────
// Maps a sleeping option's group (see ProjectNotifier.sleepingOptionGroups)
// to the dot colour used on its chip, per the TraxJourney design system.
const kSleepIndoors  = Color(0xFF3B82F6); // blue
const kSleepOutdoors = Color(0xFF22C55E); // green
const kSleepOther    = Color(0xFFA855F7); // purple

/// Dot colour for a sleeping-option group label ("Indoors"/"Outdoors"/…).
Color sleepingGroupColor(String? group) {
  switch (group) {
    case 'Indoors':
      return kSleepIndoors;
    case 'Outdoors':
      return kSleepOutdoors;
    default:
      return kSleepOther;
  }
}

// ── Metallic blue (primary action) ───────────────────────────────────────────
/// The 135° cobalt→navy gradient used on primary "metallic" buttons.
/// Tracks brightness so it matches the active Material theme.
LinearGradient metallicBlue(Brightness b) => b == Brightness.dark
    ? const LinearGradient(
        begin: Alignment.topLeft, end: Alignment.bottomRight,
        colors: [Color(0xFF60A5FA), Color(0xFF1D4ED8)])
    : const LinearGradient(
        begin: Alignment.topLeft, end: Alignment.bottomRight,
        colors: [Color(0xFF0D6EFD), Color(0xFF1E40AF)]);

/// Soft elevation shadow for floating cards / modals (design `--shadow-2`).
List<BoxShadow> kShadow2(Brightness b) => [
      BoxShadow(
        color: b == Brightness.dark
            ? const Color(0x99000000)
            : const Color(0x2E0F2236),
        blurRadius: 24,
        offset: const Offset(0, 8),
      ),
    ];

// ── Mono numerals (JetBrains Mono, tabular) ──────────────────────────────────
/// Tabular monospaced style for instrument-grade numerals (day number,
/// distances, counters). JetBrains Mono is already a bundled Google font.
TextStyle monoStyle({
  double? fontSize,
  FontWeight? fontWeight,
  Color? color,
  double? letterSpacing,
  double? height,
}) =>
    GoogleFonts.jetBrainsMono(
      fontSize: fontSize,
      fontWeight: fontWeight,
      color: color,
      letterSpacing: letterSpacing,
      height: height,
      fontFeatures: const [FontFeature.tabularFigures()],
    );
