/// Provenance the app can be asked about, not only look at (#260, unit 6).
///
/// The app has recorded where an activity came from since GPX import shipped,
/// and drew a 9 px badge for it. That badge had no accessible name — a screen
/// reader announced nothing, and a sighted user had no way to learn what it
/// meant either — it appeared in one list and nowhere else, and there was no
/// way to ask "show me the ones I imported". Provenance you cannot query is
/// provenance the app only pretends to keep.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/project_filters.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

Map<String, dynamic> _activity({
  required String day,
  String? source,
  String type = 'ride',
}) =>
    {
      'id': source == 'gpx' ? -7 : 4242,
      'name': 'An activity',
      'type': type,
      if (source != null) 'source': source,
      'start_date_local': '${day}T09:00:00Z',
    };

/// A notifier holding *real* activities and day metadata, so the filtering
/// under test is the app's own and not a copy of it restated in the test.
ProjectNotifier _notifierWith(List<Map<String, dynamic>> activities) {
  final notifier = ProjectNotifier(ProjectService());
  notifier.activities = activities;
  notifier.dayMeta = {
    for (final a in activities)
      (a['start_date_local'] as String).substring(0, 10): <String, dynamic>{},
  };
  notifier.items = [
    for (final a in activities)
      {'item_type': 'activity', 'activity_id': a['id']},
  ];
  return notifier;
}

void main() {
  group('ProjectFilters carries a source dimension', () {
    test('an empty filter set is inactive', () {
      expect(const ProjectFilters().hasActive, isFalse);
    });

    test('a source selection makes the filter active and counts', () {
      const filters = ProjectFilters(sources: {'gpx'});

      expect(filters.hasActive, isTrue);
      expect(filters.activeCount, 1);
    });

    test('it counts alongside the other dimensions', () {
      const filters =
          ProjectFilters(activityTypes: {'ride'}, sources: {'gpx', 'strava'});

      expect(filters.activeCount, 3);
    });

    test('copyWith leaves the others alone, and can clear it again', () {
      const before = ProjectFilters(tags: {'beach'}, activityTypes: {'ride'});

      final after = before.copyWith(sources: {'gpx'});

      expect(after.sources, {'gpx'});
      expect(after.tags, {'beach'});
      expect(after.activityTypes, {'ride'});
      expect(after.copyWith(sources: {}).sources, isEmpty);
    });
  });

  group('the notifier offers the sources a trip actually has', () {
    test('a Strava-only trip offers one, so the sheet knows not to ask', () {
      final notifier = _notifierWith([_activity(day: '2024-06-01')]);

      expect(notifier.availableSources, ['strava']);
    });

    test('a mixed trip offers both', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01'),
        _activity(day: '2024-06-02', source: 'gpx'),
      ]);

      expect(notifier.availableSources, ['gpx', 'strava']);
    });
  });

  group('filtering by source', () {
    // An activity with no `source` is a Strava sync: the column arrived with
    // GPX import and was left NULL for everything already there, so absence is
    // the answer rather than missing data.
    test('a null source filters as strava', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01'),
        _activity(day: '2024-06-02', source: 'gpx'),
      ]);

      notifier.setFilters(sources: {'strava'});
      expect(notifier.selectedDays, {'2024-06-01'});

      notifier.setFilters(sources: {'gpx'});
      expect(notifier.selectedDays, {'2024-06-02'});
    });

    test('an empty string is treated the same as null', () {
      final notifier = _notifierWith([_activity(day: '2024-06-01', source: '')]);

      notifier.setFilters(sources: {'strava'});

      expect(notifier.selectedDays, {'2024-06-01'});
    });

    test('a day holding both sources matches either', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01'),
        {..._activity(day: '2024-06-01', source: 'gpx'), 'id': -8},
      ]);

      notifier.setFilters(sources: {'gpx'});
      expect(notifier.selectedDays, {'2024-06-01'});

      notifier.setFilters(sources: {'strava'});
      expect(notifier.selectedDays, {'2024-06-01'});
    });

    test('selecting both is not the same as selecting neither', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01'),
        _activity(day: '2024-06-02', source: 'gpx'),
      ]);

      notifier.setFilters(sources: {'strava', 'gpx'});
      expect(notifier.selectedDays, {'2024-06-01', '2024-06-02'});

      notifier.setFilters(sources: {});
      expect(notifier.selectedDays, isEmpty);
      expect(notifier.hasActiveFilter, isFalse);
    });

    test('it narrows alongside another dimension rather than replacing it', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01', type: 'ride'),
        _activity(day: '2024-06-02', source: 'gpx', type: 'hike'),
      ]);

      notifier.setFilters(activityTypes: {'ride'}, sources: {'gpx'});

      // Day 1 is a ride but from Strava; day 2 is GPX but a hike. Each arm is
      // an AND across dimensions, so neither day satisfies both.
      expect(notifier.selectedDays, isEmpty);
    });

    test('a saved source the trip no longer holds is dropped on restore', () {
      // Import a track, filter the trip to it, then delete it: the saved
      // 'gpx' would come back on the next load, match nothing, and empty the
      // whole list — with the sheet's Source section gone, because the trip is
      // down to one source again.
      final notifier = _notifierWith([_activity(day: '2024-06-01')]);

      notifier.restoreFilters(const ProjectFilters(sources: {'gpx'}));

      expect(notifier.sourceFilter, isEmpty);
      expect(notifier.hasActiveFilter, isFalse);
      expect(notifier.selectedDays, isEmpty);
    });

    test('a saved source the trip does hold is applied', () {
      final notifier = _notifierWith([
        _activity(day: '2024-06-01'),
        _activity(day: '2024-06-02', source: 'gpx'),
      ]);

      notifier.restoreFilters(const ProjectFilters(sources: {'gpx'}));

      expect(notifier.sourceFilter, {'gpx'});
      expect(notifier.selectedDays, {'2024-06-02'});
    });

    test('the guard reaches every dimension, not the source alone', () {
      // Until #409 this asserted the opposite: tags restored as saved while a
      // stale source was dropped, because tags match on *effective* tags and
      // it was not yet shown that pruning them against availableTags is safe.
      // It is — stale_filter_restore_test.dart pins why — so the line now
      // falls past every dimension, and a stale value in any of them is
      // dropped and reported.
      final notifier = _notifierWith([_activity(day: '2024-06-01')]);

      final pruned = notifier.restoreFilters(const ProjectFilters(
        tags: {'beach'},
        sleeping: {'Hotel'},
        activityTypes: {'hike'},
        transport: {'flight'},
        sources: {'gpx'},
      ));

      expect(pruned, isTrue);
      expect(notifier.tagFilter, isEmpty);
      expect(notifier.sleepingFilter, isEmpty);
      expect(notifier.activityTypeFilter, isEmpty);
      expect(notifier.transportFilter, isEmpty);
      expect(notifier.sourceFilter, isEmpty);
      expect(notifier.hasActiveFilter, isFalse);
    });

    test('and reports nothing pruned when every value still resolves', () {
      // A day with no sleeping mode filters as 'No data', so that is held too.
      final notifier = _notifierWith([_activity(day: '2024-06-01')]);

      final pruned = notifier.restoreFilters(const ProjectFilters(
        sleeping: {'No data'},
        activityTypes: {'ride'},
        sources: {'strava'},
      ));

      expect(pruned, isFalse);
      expect(notifier.activeFilterCount, 3);
      expect(notifier.selectedDays, {'2024-06-01'});
    });

    test('clearing every filter clears the source too', () {
      final notifier = _notifierWith([_activity(day: '2024-06-01', source: 'gpx')]);
      notifier.setFilters(sources: {'gpx'});

      notifier.clearAllFilters();

      expect(notifier.sourceFilter, isEmpty);
      expect(notifier.hasActiveFilter, isFalse);
    });
  });
}
