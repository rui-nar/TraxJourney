part of 'project_facet.dart';

/// The trip's content: activities, items, people, groups, day-meta, trip dates,
/// sleeping options and counters, with what is derived from them alone (the
/// filter options, the day list and the per-day stats).
///
/// Besides [version], which goes up on every write, four versions say which
/// part changed (Decision 23 of docs/CLIENT_STATE_MAP_PLAN.md), so a widget
/// can key a cache on the part it draws:
/// - [listVersion]: the item list, and which of its fields stayed encrypted;
/// - [activitiesVersion]: the activity data, elevation merges included;
/// - [peopleVersion]: people and groups;
/// - [dayMetaVersion]: day-meta, the trip dates, the sleeping options and
///   their groups, and the counters.
final class ItemsFacet extends ProjectFacet {
  ItemsFacet._();

  List<Map<String, dynamic>> _activities = [];
  List<Map<String, dynamic>> _items = [];
  List<Map<String, dynamic>> _people = [];
  List<Map<String, dynamic>> _groups = [];
  final UndecryptedFields _undecrypted = UndecryptedFields();
  String? _tripStart;
  String? _tripEnd;
  Map<String, Map<String, dynamic>> _dayMeta = {};
  List<String> _sleepingOptions = [];
  Map<String, String> _sleepingOptionGroups = {};
  List<Map<String, dynamic>> _counters = [];

  int _listVersion = 0;
  int _activitiesVersion = 0;
  int _peopleVersion = 0;
  int _dayMetaVersion = 0;

  List<Map<String, dynamic>> get activities => _activities;

  /// Ordered project items (activities + segments + memories + journals).
  List<Map<String, dynamic>> get items => _items;

  /// Trip people directory (#40).
  List<Map<String, dynamic>> get people => _people;

  /// People groups (#50).
  List<Map<String, dynamic>> get groups => _groups;

  /// Memory/journal fields the notifier's reveal left as ciphertext (#466).
  UndecryptedFields get undecryptedFields => _undecrypted;

  /// User-defined trip start date override ("YYYY-MM-DD"); null = infer from activities.
  String? get tripStart => _tripStart;

  /// User-defined trip end date ("YYYY-MM-DD"); null = trip still ongoing.
  String? get tripEnd => _tripEnd;

  /// Day metadata keyed by "YYYY-MM-DD".
  Map<String, Map<String, dynamic>> get dayMeta => _dayMeta;

  /// Project-specific list of sleeping type options.
  List<String> get sleepingOptions => _sleepingOptions;

  /// Group assignment for each sleeping option: name → "Outdoors"|"Indoors"|"Other".
  Map<String, String> get sleepingOptionGroups => _sleepingOptionGroups;

  /// Project-defined counters: [{name: String, start: double}].
  List<Map<String, dynamic>> get counters => _counters;

  /// Goes up when the item list changes.
  int get listVersion => _listVersion;

  /// Goes up when the activity data changes, elevation merges included.
  int get activitiesVersion => _activitiesVersion;

  /// Goes up when people or groups change.
  int get peopleVersion => _peopleVersion;

  /// Goes up when day-meta, the trip dates, the sleeping options or the
  /// counters change.
  int get dayMetaVersion => _dayMetaVersion;

  // Marks the facet changed, and the parts named.
  void _mark({
    bool list = false,
    bool activities = false,
    bool people = false,
    bool dayMeta = false,
  }) {
    // As [_markChanged]: a write after the notifier was discarded counts
    // for nothing.
    if (_disposed) return;
    if (list) _listVersion++;
    if (activities) _activitiesVersion++;
    if (people) _peopleVersion++;
    if (dayMeta) _dayMetaVersion++;
    _markChanged();
  }

  // ── Filter options (derived from the trip's content) ──────────────────────

  bool get hasFilterableContent =>
      availableTags.isNotEmpty || availableSleepingModes.isNotEmpty ||
      availableActivityTypes.isNotEmpty || availableTransportationMeans.isNotEmpty;

  List<String> get availableTags {
    final s = <String>{};
    for (final m in _dayMeta.values) {
      final t = m['tags'];
      if (t is List) s.addAll(t.cast<String>());
    }
    return s.toList()..sort();
  }

  /// Tags shown for [dateKey] under the "inherit from the previous day" rule
  /// (issue #18): a day with its own tags keeps them; a day with none falls
  /// back to the nearest strictly-earlier day that has tags. See
  /// [effectiveDayTags] for the gap-skipping semantics.
  List<String> effectiveTagsFor(String dateKey) =>
      effectiveDayTags(_dayMeta, dateKey);

  /// Whether [dateKey] carries tags of its own (vs only inherited ones) — true
  /// for an explicit empty set too, since that means "no tags, don't inherit"
  /// rather than "no data" (issue #203). Lets the UI render inherited tags
  /// faded and distinguish them from real ones. The same test as
  /// [effectiveDayTags] makes before it inherits.
  bool dayHasOwnTags(String dateKey) => _dayMeta[dateKey]?['tags'] is List;

  List<String> get availableSleepingModes {
    final s = <String>{};
    bool hasNoData = false;
    for (final m in _dayMeta.values) {
      final v = m['sleeping'] as String?;
      if (v != null && v.isNotEmpty) {
        s.add(v);
      } else {
        hasNoData = true;
      }
    }
    final result = s.toList()..sort();
    if (hasNoData) result.add('No data');
    return result;
  }

  List<String> get availableActivityTypes {
    final s = <String>{};
    for (final a in _activities) {
      final t = (a['type'] as String? ?? '').toLowerCase();
      if (t.isNotEmpty) s.add(t);
    }
    return s.toList()..sort();
  }

  /// The sources this trip's activities actually came from.
  ///
  /// An activity with no `source` is a Strava sync — the column arrived with
  /// GPX import and was left NULL for everything already there, so absence is
  /// the answer rather than missing data. Returns a single entry for a trip
  /// that came from one place, which is how the filter sheet knows not to ask.
  List<String> get availableSources {
    final s = <String>{};
    for (final a in _activities) {
      final source = a['source'] as String?;
      s.add(source == null || source.isEmpty ? 'strava' : source);
    }
    return s.toList()..sort();
  }

  List<String> get availableTransportationMeans {
    final s = <String>{};
    for (final item in _items) {
      if (item['item_type'] != 'segment') continue;
      final t = (item['segment'] as Map?)?['segment_type'] as String?;
      if (t != null && t.isNotEmpty) s.add(t);
    }
    return s.toList()..sort();
  }

  // ── Day stats and the day list (memoised) ─────────────────────────────────

  // Raw (pre-/1000) meters per day — divided down to km only when read, so
  // caching this can't shift the float rounding of the original single
  // divide-at-the-end computation below.
  Map<String, ({double distanceM, double elevationM})>? _dayStatsCache;
  List<Map<String, dynamic>>? _dayStatsCacheItems;
  List<Map<String, dynamic>>? _dayStatsCacheActivities;

  /// Distance (km) and climb (m) summed over the activities on [dateKey]
  /// ("YYYY-MM-DD"). An activity belongs to the day of its
  /// `start_date_local` — the same rule the activity panel groups by — so the
  /// totals match what the day header shows. Returns zeros for a day with no
  /// activities (the Edit Day hero then hides its stat strip).
  ///
  /// The day carousel calls this once per visible day on every rebuild —
  /// including every rebuild a day *selection* triggers — so recomputing it
  /// with a fresh O(activities) scan of `items` each time compounds with the
  /// map's own per-selection rebuild cost (see map_panel.dart's
  /// buildDayIndex). Cached here instead: one O(items) pass builds stats for
  /// every day at once, reused until `items`/`activities` actually change.
  ({double distanceKm, double elevationM}) dayStats(String dateKey) {
    if (!identical(_items, _dayStatsCacheItems) ||
        !identical(_activities, _dayStatsCacheActivities)) {
      final byId = {for (final a in _activities) a['id']?.toString(): a};
      final cache = <String, ({double distanceM, double elevationM})>{};
      for (final item in _items) {
        if (item['item_type'] != 'activity') continue;
        final a = byId[item['activity_id']?.toString()];
        if (a == null) continue;
        final ds = (a['start_date_local'] as String?)?.split('T').first;
        if (ds == null) continue;
        final prev = cache[ds] ?? (distanceM: 0.0, elevationM: 0.0);
        cache[ds] = (
          distanceM: prev.distanceM + (a['distance'] as num? ?? 0).toDouble(),
          elevationM: prev.elevationM +
              (a['total_elevation_gain'] as num? ?? 0).toDouble(),
        );
      }
      _dayStatsCache = cache;
      _dayStatsCacheItems = _items;
      _dayStatsCacheActivities = _activities;
    }
    final entry = _dayStatsCache![dateKey];
    return entry == null
        ? (distanceKm: 0.0, elevationM: 0.0)
        : (distanceKm: entry.distanceM / 1000.0, elevationM: entry.elevationM);
  }

  List<String>? _orderedDayKeysCache;
  Map<String, Map<String, dynamic>>? _orderedDayKeysCacheDayMeta;
  List<Map<String, dynamic>>? _orderedDayKeysCacheActivities;
  List<Map<String, dynamic>>? _orderedDayKeysCacheItems;

  /// Every day key ("YYYY-MM-DD") the project touches, ascending: the union of
  /// day-meta days and the days any dated content falls on ([contentDayKeys] —
  /// activities plus memories, journals, encounters and segments). That is the
  /// same bucketing the activity panel gives a day header to, so a day the
  /// panel shows is a day this lists (issue #370). It is the full-trip day
  /// list regardless of any active filter (unlike the activity panel's
  /// display-derived list), so it's safe to use from the add-FAB.
  ///
  /// Called from several places on every selection-triggered rebuild — the
  /// day carousel, computeSelectionStats, activeDayKey — each a fresh
  /// O(activities + items) scan before this cache existed. Same
  /// identical()-based convention as dayStats above.
  List<String> orderedDayKeys() {
    if (!identical(_dayMeta, _orderedDayKeysCacheDayMeta) ||
        !identical(_activities, _orderedDayKeysCacheActivities) ||
        !identical(_items, _orderedDayKeysCacheItems)) {
      final keys = <String>{
        ..._dayMeta.keys,
        ...contentDayKeys(_activities, _items),
      };
      _orderedDayKeysCache = keys.toList()..sort();
      _orderedDayKeysCacheDayMeta = _dayMeta;
      _orderedDayKeysCacheActivities = _activities;
      _orderedDayKeysCacheItems = _items;
    }
    return _orderedDayKeysCache!;
  }

  void _reset() {
    _activities = [];
    _items = [];
    _people = [];
    _groups = [];
    _undecrypted.reset();
    _tripStart = null;
    _tripEnd = null;
    _dayMeta = {};
    _sleepingOptions = [];
    _sleepingOptionGroups = {};
    _counters = [];
    // Memos keyed on the identity of the lists dropped above. They would
    // miss anyway, but holding them kept the last trip alive in memory.
    _dayStatsCache = null;
    _dayStatsCacheItems = null;
    _dayStatsCacheActivities = null;
    _orderedDayKeysCache = null;
    _orderedDayKeysCacheDayMeta = null;
    _orderedDayKeysCacheActivities = null;
    _orderedDayKeysCacheItems = null;
    // Every part changed. The versions go on up rather than back to 0, so a
    // cache keyed on one never mistakes the cleared state for an earlier one.
    _mark(list: true, activities: true, people: true, dayMeta: true);
  }
}

/// Writes [ItemsFacet]. See `project_facet.dart` for who may hold one.
///
/// Every write marks the part it belongs to. The lists and maps are replaced
/// whole, never changed in place, so a reader keyed on a value's identity
/// sees each write as a new value.
final class ItemsFacetWriter extends ProjectFacetWriter<ItemsFacet> {
  ItemsFacetWriter() : super(ItemsFacet._());

  void setActivities(List<Map<String, dynamic>> v) {
    facet._activities = v;
    facet._mark(activities: true);
  }

  void setItems(List<Map<String, dynamic>> v) {
    facet._items = v;
    facet._mark(list: true);
  }

  void setPeople(List<Map<String, dynamic>> v) {
    facet._people = v;
    facet._mark(people: true);
  }

  void setGroups(List<Map<String, dynamic>> v) {
    facet._groups = v;
    facet._mark(people: true);
  }

  /// Replaces the record of fields left encrypted with [undecrypted], as
  /// `(kind, id, field)` triples from a reveal of the whole item list.
  void recordUndecrypted(List<(String, String, String)> undecrypted) {
    facet._undecrypted.reset();
    for (final (kind, id, field) in undecrypted) {
      facet._undecrypted.mark(kind, id, field);
    }
    facet._mark(list: true);
  }

  /// The user saved [field] of the [kind] item [id]: it now holds what they
  /// typed, not ciphertext.
  void forgetUndecrypted(String kind, String id, String field) {
    facet._undecrypted.remove(kind, id, field);
    facet._mark(list: true);
  }

  void setTripDates(String? start, String? end) {
    facet._tripStart = start;
    facet._tripEnd = end;
    facet._mark(dayMeta: true);
  }

  void setDayMeta(Map<String, Map<String, dynamic>> v) {
    facet._dayMeta = v;
    facet._mark(dayMeta: true);
  }

  void setSleepingOptions(List<String> v) {
    facet._sleepingOptions = v;
    facet._mark(dayMeta: true);
  }

  void setSleepingOptionGroups(Map<String, String> v) {
    facet._sleepingOptionGroups = v;
    facet._mark(dayMeta: true);
  }

  void setCounters(List<Map<String, dynamic>> v) {
    facet._counters = v;
    facet._mark(dayMeta: true);
  }

  // Single-field writes, for tests.
  void setTripStart(String? v) {
    facet._tripStart = v;
    facet._mark(dayMeta: true);
  }

  void setTripEnd(String? v) {
    facet._tripEnd = v;
    facet._mark(dayMeta: true);
  }

  @override
  void reset() => facet._reset();
}
