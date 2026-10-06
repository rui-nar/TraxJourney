part of 'project_facet.dart';

/// What the user has selected: the selected item and day, the filters and
/// whether journals show.
final class SelectionFacet extends ProjectFacet {
  SelectionFacet._();

  dynamic _activityId;
  dynamic _segmentId;
  dynamic _memoryId;
  dynamic _journalId;
  bool _showJournals = true;
  String? _day;
  Set<String> _days = {};
  ProjectFilters _filters = ProjectFilters.empty;

  /// The activity currently highlighted on the map. Null = no selection.
  dynamic get selectedActivityId => _activityId;

  /// The connecting segment currently highlighted on the map. Null = no selection.
  dynamic get selectedSegmentId => _segmentId;

  /// The memory currently highlighted on the map/panel. Null = no selection.
  dynamic get selectedMemoryId => _memoryId;

  /// The journal entry currently highlighted on the map/panel. Null = no selection.
  dynamic get selectedJournalId => _journalId;

  /// Whether journal markers and list items are visible.
  bool get showJournals => _showJournals;

  /// The day currently selected in the activity panel ("YYYY-MM-DD" or null).
  String? get selectedDay => _day;

  /// Days selected in multi-select mode, or the days matching the filters.
  /// Empty = no multi-day filter.
  Set<String> get selectedDays => _days;

  ProjectFilters get filters => _filters;

  // Backwards-compat shims — the widget call sites read these by name.
  Set<String> get tagFilter => _filters.tags;
  Set<String> get sleepingFilter => _filters.sleeping;
  Set<String> get activityTypeFilter => _filters.activityTypes;
  Set<String> get sourceFilter => _filters.sources;
  Set<String> get transportFilter => _filters.transport;
  int get activeFilterCount => _filters.activeCount;
  bool get hasActiveFilter => _filters.hasActive;

  // The id [id] selects: itself, or null when [current] is already it.
  static dynamic _toggled(dynamic current, dynamic id) =>
      current?.toString() == id?.toString() ? null : id;

  // One item selected at a time: the four item slots, the day and the days.
  void _selectItem({
    dynamic activity,
    dynamic segment,
    dynamic memory,
    dynamic journal,
  }) {
    _activityId = activity;
    _segmentId = segment;
    _memoryId = memory;
    _journalId = journal;
    _day = null;
    _days = {};
    _markChanged();
  }

  void _selectActivity(dynamic id) =>
      _selectItem(activity: _toggled(_activityId, id));
  void _selectSegment(dynamic id) =>
      _selectItem(segment: _toggled(_segmentId, id));
  void _selectMemory(dynamic id) =>
      _selectItem(memory: _toggled(_memoryId, id));
  void _selectJournal(dynamic id) =>
      _selectItem(journal: _toggled(_journalId, id));

  void _toggleJournals() {
    _showJournals = !_showJournals;
    _markChanged();
  }

  void _selectDay(String? dateKey) {
    _day = dateKey;
    _activityId = null;
    _segmentId = null;
    _memoryId = null;
    _days = {};
    _markChanged();
  }

  void _selectDays(Set<String> days) {
    _days = Set.from(days);
    _activityId = null;
    _segmentId = null;
    _memoryId = null;
    _day = null;
    _markChanged();
  }

  void _clearItemSelection() {
    _day = null;
    _activityId = null;
    _segmentId = null;
    _memoryId = null;
    _markChanged();
  }

  void _clearAllSelection() {
    _days = {};
    _clearItemSelection();
  }

  void _setFilters(ProjectFilters filters, Set<String> days) {
    _filters = filters;
    _days = days;
    _markChanged();
  }

  void _reset() {
    _activityId = null;
    _segmentId = null;
    _memoryId = null;
    _journalId = null;
    _showJournals = true;
    _day = null;
    _days = {};
    _filters = ProjectFilters.empty;
    _markChanged();
  }
}

/// Writes [SelectionFacet]. See `project_facet.dart` for who may hold one.
final class SelectionFacetWriter extends ProjectFacetWriter<SelectionFacet> {
  SelectionFacetWriter() : super(SelectionFacet._());

  /// Highlights activity [id], or clears it when it already is, and clears
  /// every other selection.
  void selectActivity(dynamic id) => facet._selectActivity(id);

  /// As [selectActivity], for a segment.
  void selectSegment(dynamic id) => facet._selectSegment(id);

  /// As [selectActivity], for a memory.
  void selectMemory(dynamic id) => facet._selectMemory(id);

  /// As [selectActivity], for a journal entry.
  void selectJournal(dynamic id) => facet._selectJournal(id);

  void toggleJournals() => facet._toggleJournals();

  /// Selects [dateKey], clearing the item and multi-day selections.
  void selectDay(String? dateKey) => facet._selectDay(dateKey);

  /// Selects [days], clearing the item and single-day selections.
  void selectDays(Set<String> days) => facet._selectDays(days);

  /// Clears the selected day, activity, segment and memory.
  void clearItemSelection() => facet._clearItemSelection();

  /// As [clearItemSelection], and the selected days too.
  void clearAllSelection() => facet._clearAllSelection();

  /// Replaces the filters and the days they match, together.
  void setFilters(ProjectFilters filters, Set<String> days) =>
      facet._setFilters(filters, days);

  // Single-field writes, for restoring saved state and for tests.
  void setSelectedDay(String? v) {
    facet._day = v;
    markChanged();
  }

  void setSelectedDays(Set<String> v) {
    facet._days = v;
    markChanged();
  }

  void setSelectedActivityId(dynamic v) {
    facet._activityId = v;
    markChanged();
  }

  void setSelectedSegmentId(dynamic v) {
    facet._segmentId = v;
    markChanged();
  }

  void setSelectedMemoryId(dynamic v) {
    facet._memoryId = v;
    markChanged();
  }

  @override
  void reset() => facet._reset();
}
