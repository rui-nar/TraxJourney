/// Mixin that provides all filter state and logic to ProjectNotifier.
///
/// Abstract getters/setters declared here are satisfied automatically by
/// ProjectNotifier's existing fields — no boilerplate needed in the class.
library;

import 'package:flutter/foundation.dart';

import 'facets/project_facet.dart';
import 'project_filters.dart';

mixin ProjectFilterMixin on ChangeNotifier {
  // ── Abstract: project data (the notifier's ItemsFacet) ──────────────────
  ItemsFacetWriter get itemsFacetWriter;

  // ── Abstract: selection state (the notifier's SelectionFacet) ────────────
  // setFilters clears item selection so a filtered-out item isn't left active.
  SelectionFacetWriter get selectionFacetWriter;

  // ── Abstract: UI-state persistence hook (issue #76 follow-up) ─────────────
  // Implemented by ProjectNotifier — persists selection + filter state to
  // shared_preferences so a forced reload doesn't lose it.
  void saveUiState();

  // ── Filter state (held by the SelectionFacet) ────────────────────────────
  ProjectFilters get _filters => selectionFacetWriter.facet.filters;

  // The trip's content, and the filter options derived from it (#294).
  ItemsFacet get _content => itemsFacetWriter.facet;

  // ── Mutators ──────────────────────────────────────────────────────────────

  void setFilters({
    Set<String>? tags,
    Set<String>? sleeping,
    Set<String>? activityTypes,
    Set<String>? transport,
    Set<String>? sources,
  }) {
    final next = _filters.copyWith(
      tags: tags,
      sleeping: sleeping,
      activityTypes: activityTypes,
      transport: transport,
      sources: sources,
    );
    selectionFacetWriter.setFilters(next, _matchingDays(next));
    selectionFacetWriter.clearItemSelection();
    saveUiState();
    notifyListeners();
  }

  void clearAllFilters() => setFilters(
      tags: {}, sleeping: {}, activityTypes: {}, transport: {}, sources: {});

  /// Resets filter state to empty. Called by ProjectNotifier.clear().
  void resetFilters() {
    selectionFacetWriter.setFilters(ProjectFilters.empty, {});
  }

  /// Applies a filter set restored from shared_preferences (issue #76
  /// follow-up) without the selection-clearing side effect [setFilters] has —
  /// restore needs to apply filters and selections independently.
  /// Returns true when it dropped something from [restored], so the caller can
  /// write the pruned set back: left on disk, a stale value re-applies itself
  /// the next time the trip gains matching data again.
  ///
  /// [prune] false applies [restored] verbatim and returns false — for data
  /// that cannot be trusted to say what the trip holds (an offline snapshot).
  bool restoreFilters(ProjectFilters restored, {bool prune = true}) {
    if (!prune) {
      selectionFacetWriter.setFilters(restored, _matchingDays(restored));
      return false;
    }

    // A value the trip no longer holds is dropped rather than applied, in every
    // dimension. Filter a trip to hikes and delete the last hike: the saved
    // 'hike' would match no day and empty the list, and the sheet, which
    // offers what the trip holds, would have no chip to untick it. Same
    // reasoning as the stale day/activity references _restoreUiState already
    // drops (#260, #409).
    //
    // Pruning against the available* getters never drops a value that still
    // matches a day: each is what the sheet offers as chips, derived from the
    // same fields _recomputeSelectedDays compares with the same normalisation
    // (lower-cased types, 'No data' for an unset sleeping mode). Tags are
    // the one that needs an argument, because they match on *effective* tags —
    // but an inherited tag is always some earlier day's own tag, and a day's
    // own tags are its effective tags, so the effective tags across the trip
    // are exactly availableTags. The restore tests pin that equivalence.
    Set<String> held(Set<String> saved, List<String> available) =>
        saved.where(available.contains).toSet();

    final kept = restored.copyWith(
      tags: held(restored.tags, _content.availableTags),
      sleeping: held(restored.sleeping, _content.availableSleepingModes),
      activityTypes: held(restored.activityTypes, _content.availableActivityTypes),
      transport: held(restored.transport, _content.availableTransportationMeans),
      sources: held(restored.sources, _content.availableSources),
    );
    selectionFacetWriter.setFilters(kept, _matchingDays(kept));
    // Every dimension only ever shrinks, so the count moves iff something went.
    return kept.activeCount != restored.activeCount;
  }

  // ── Internal ──────────────────────────────────────────────────────────────

  /// The days [filters] match; empty when none is set.
  Set<String> _matchingDays(ProjectFilters filters) {
    if (!filters.hasActive) return {};

    final actByDay = <String, Set<String>>{};
    for (final a in _content.activities) {
      final d = (a['start_date_local'] as String?)?.substring(0, 10);
      final t = (a['type'] as String? ?? '').toLowerCase();
      if (d != null && t.isNotEmpty) (actByDay[d] ??= {}).add(t);
    }

    // Where the day's activities came from. An activity with no `source`
    // is a Strava sync: that column was added by GPX import and left NULL
    // for everything that already existed, so absence is the answer
    // rather than missing data.
    final srcByDay = <String, Set<String>>{};
    for (final a in _content.activities) {
      final d = (a['start_date_local'] as String?)?.substring(0, 10);
      if (d == null) continue;
      final source = a['source'] as String?;
      (srcByDay[d] ??= {}).add(
          source == null || source.isEmpty ? 'strava' : source);
    }

    final trByDay = <String, Set<String>>{};
    for (final item in _content.items) {
      if (item['item_type'] != 'segment') continue;
      final seg = item['segment'] as Map?;
      final d = seg?['date'] as String?;
      final t = seg?['segment_type'] as String?;
      if (d != null && t != null) (trByDay[d] ??= {}).add(t);
    }

    final matching = <String>{};
    for (final dk in _content.dayMeta.keys) {
      if (filters.tags.isNotEmpty) {
        // Match on *effective* tags so days that only inherit a tag from an
        // earlier day still satisfy the tag filter (issue #18).
        final tags = effectiveDayTags(_content.dayMeta, dk).toSet();
        if (!tags.any(filters.tags.contains)) continue;
      }
      if (filters.sleeping.isNotEmpty) {
        final s = _content.dayMeta[dk]?['sleeping'] as String?;
        final label = (s == null || s.isEmpty) ? 'No data' : s;
        if (!filters.sleeping.contains(label)) continue;
      }
      if (filters.activityTypes.isNotEmpty) {
        final types = actByDay[dk] ?? const {};
        if (!types.any(filters.activityTypes.contains)) continue;
      }
      if (filters.transport.isNotEmpty) {
        final types = trByDay[dk] ?? const {};
        if (!types.any(filters.transport.contains)) continue;
      }
      if (filters.sources.isNotEmpty) {
        final sources = srcByDay[dk] ?? const {};
        if (!sources.any(filters.sources.contains)) continue;
      }
      matching.add(dk);
    }
    return matching;
  }
}

// ── Pure tag-inheritance helpers (issue #18) ─────────────────────────────────
//
// Kept as free functions so they can be unit-tested without building a
// ProjectNotifier. Date keys are "YYYY-MM-DD", so lexicographic string order is
// chronological order — no DateTime parsing needed.

/// Whether [dateKey] has an explicit 'tags' entry of its own — including an
/// empty one. An empty-but-present list means "this day has no tags, don't
/// inherit" (issue #203); an absent key means "no data, please inherit".
bool _hasOwnTagsKey(
  Map<String, Map<String, dynamic>> dayMeta,
  String dateKey,
) =>
    dayMeta[dateKey]?['tags'] is List;

/// The tags a day owns outright (an empty list if it has none of its own).
List<String> _ownDayTags(
  Map<String, Map<String, dynamic>> dayMeta,
  String dateKey,
) {
  final raw = dayMeta[dateKey]?['tags'];
  return raw is List ? raw.cast<String>() : const <String>[];
}

/// Effective tags for [dateKey] under the "inherit from the previous day" rule
/// (issue #18, "live fallback" model):
///
/// * a day with its own tags — even an explicit empty set — shows exactly
///   those and never inherits (issue #203: clearing every tag on a day must
///   stick, not silently fall back to an earlier day's tags);
/// * a day with no tags data at all falls back to the tags of the nearest
///   *strictly earlier* day that has (non-empty) tags of its own — empty/gap
///   days in between are skipped (so a gap day never blanks out the
///   inheritance chain);
/// * a day with no own tags and no earlier tagged day shows nothing.
///
/// Inherited tags are never persisted: they vanish the moment the source day's
/// tags change, and a day only "owns" tags once the user edits it.
///
/// `ItemsFacet.effectiveTagsFor` is its caller in the app.
List<String> effectiveDayTags(
  Map<String, Map<String, dynamic>> dayMeta,
  String dateKey,
) {
  if (_hasOwnTagsKey(dayMeta, dateKey)) return _ownDayTags(dayMeta, dateKey);

  String? best;
  for (final k in dayMeta.keys) {
    if (k.compareTo(dateKey) >= 0) continue; // must be strictly earlier
    if (_ownDayTags(dayMeta, k).isEmpty) continue; // must own non-empty tags
    if (best == null || k.compareTo(best) > 0) best = k; // keep the latest
  }
  return best == null ? const <String>[] : _ownDayTags(dayMeta, best);
}
