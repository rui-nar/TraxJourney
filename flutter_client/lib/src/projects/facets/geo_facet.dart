part of 'project_facet.dart';

/// What kind of geometry a [GeoFacet] holds. See [GeoLod].
enum GeoLodKind {
  /// Nothing: before a load's first geometry, or after `clear()`.
  none,

  /// The straight-line approximation a load shows first: the server's
  /// low-res endpoint, or built client-side for an E2EE trip.
  lowRes,

  /// Simplified by the server for one zoom bucket (issue #295), and scoped to
  /// a box or not (issue #324).
  level,

  /// Full resolution: an E2EE trip's client-built geometry, or the offline
  /// cache and older-server fallbacks.
  full,
}

/// The level of detail the geometry on screen was built for (issue #379,
/// Decision 10 of docs/CLIENT_STATE_MAP_PLAN.md).
///
/// It travels with the geometry: [GeoFacetWriter.replace] takes both, so the
/// two can no longer disagree — which is how a post-mutation reload could hold
/// full-resolution geometry under a zoom bucket that said otherwise.
@immutable
final class GeoLod {
  const GeoLod._(this.kind)
      : bucket = null,
        box = null;

  /// Simplified for zoom [bucket] — the whole level the server quantises to,
  /// `ceil(zoom)` — and scoped to [box], or covering the whole trip when null.
  /// A whole-trip answer contains every viewport, so it is never stale for
  /// the camera's position, only for its level.
  const GeoLod.level(int this.bucket, {this.box}) : kind = GeoLodKind.level;

  static const none = GeoLod._(GeoLodKind.none);
  static const lowRes = GeoLod._(GeoLodKind.lowRes);
  static const full = GeoLod._(GeoLodKind.full);

  final GeoLodKind kind;

  /// The zoom bucket of a [GeoLodKind.level], null for every other kind.
  final int? bucket;

  /// The box a [GeoLodKind.level] was fetched for, null when it covers the
  /// whole trip and for every other kind.
  final GeoBox? box;

  @override
  bool operator ==(Object other) =>
      other is GeoLod &&
      other.kind == kind &&
      other.bucket == bucket &&
      other.box == box;

  @override
  int get hashCode => Object.hash(kind, bucket, box);

  @override
  String toString() => switch (kind) {
        GeoLodKind.level => 'GeoLod.level($bucket${box == null ? '' : ', $box'})',
        _ => 'GeoLod.${kind.name}',
      };
}

/// The trip's map geometry, the level of detail it holds, and whether the
/// load's geometry phase has finished.
final class GeoFacet extends ProjectFacet {
  GeoFacet._();

  Map<String, dynamic>? _geo;
  GeoLod _lod = GeoLod.none;
  int _servedFrom = 0;
  bool _isLoaded = true;

  /// The GeoJSON FeatureCollection the map draws, null before a load's first
  /// geometry.
  Map<String, dynamic>? get geo => _geo;

  /// What [geo] was built for.
  GeoLod get lod => _lod;

  /// When the request whose answer [geo] is started, on the geo request clock
  /// of `ProjectSegmentCrudMixin.fetchServerGeo`; 0 when [geo] came from no
  /// request (a client-side build, the offline cache, nothing yet). An answer
  /// to a request that started no later than this is older than what is on
  /// screen and is not applied (Decision 24).
  int get servedFrom => _servedFrom;

  /// Whether the progressive load's geometry phase has finished. True outside
  /// a progressive load (view and shared mode start it false), so screens
  /// that use the base load see no loading state for it.
  bool get isLoaded => _isLoaded;

  bool _replace(Map<String, dynamic>? geo, GeoLod lod, int? servedFrom) {
    if (servedFrom != null && servedFrom <= _servedFrom) return false;
    _geo = geo;
    _lod = lod;
    _servedFrom = servedFrom ?? 0;
    _markChanged();
    return true;
  }

  void _replaceKeepingLod(Map<String, dynamic> geo) {
    _geo = geo;
    _markChanged();
  }

  void _forgetBox() {
    final bucket = _lod.bucket;
    if (bucket == null || _lod.box == null) return;
    _lod = GeoLod.level(bucket);
    _markChanged();
  }

  void _setLoaded(bool loaded) {
    if (_isLoaded == loaded) return;
    _isLoaded = loaded;
    _markChanged();
  }

  void _reset() {
    _geo = null;
    _lod = GeoLod.none;
    _servedFrom = 0;
    _isLoaded = true;
    _markChanged();
  }
}

/// Writes [GeoFacet]. See `project_facet.dart` for who may hold one.
///
/// Every geometry write goes through [replace] or [replaceKeepingLod], so a
/// geometry and the level it was built for are always written together.
final class GeoFacetWriter extends ProjectFacetWriter<GeoFacet> {
  GeoFacetWriter() : super(GeoFacet._());

  /// Shows [geo], built for [lod].
  ///
  /// [servedFrom] is when the request [geo] answers started, as
  /// `fetchServerGeo` returns it. An answer that is not newer than the one on
  /// screen is ignored and this returns false: a zoom or level-of-detail
  /// request still in flight across a write must not put back the geometry
  /// the post-write refresh replaced (Decision 24). A write that answers no
  /// request — clearing, a client-side build, the offline cache — passes none
  /// and is always applied; what it shows counts as older than any request.
  bool replace(Map<String, dynamic>? geo, GeoLod lod, {int? servedFrom}) =>
      facet._replace(geo, lod, servedFrom);

  /// Shows [geo], a local edit of the geometry on screen — a segment patch —
  /// which keeps its level of detail and the request it answers.
  void replaceKeepingLod(Map<String, dynamic> geo) =>
      facet._replaceKeepingLod(geo);

  /// Treats the level on screen as covering the whole trip: the zoom refetch's
  /// self-healing disarm (issue #332).
  void forgetBox() => facet._forgetBox();

  /// Records whether the progressive load's geometry phase has finished.
  void setLoaded(bool loaded) => facet._setLoaded(loaded);

  @override
  void reset() => facet._reset();
}
