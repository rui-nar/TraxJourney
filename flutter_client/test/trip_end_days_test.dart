// Unit tests for the rules behind the "Remove days after the end date?"
// confirmation in project settings (issue #358).
//
// The bug the confirmation had: it counted day-meta keys only, so it claimed
// days would disappear that the trip's day list pins via an activity or a
// memory. classifyTripEndOrphans splits the two so the dialog can say which
// days actually go and which stay.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/trip_end_days.dart';

Map<String, dynamic> _activity(String date) => {'start_date_local': date};

Map<String, dynamic> _memory(String date) => _item('memory', date);

Map<String, dynamic> _item(String type, String date) => {
      'item_type': type,
      type: {'id': '$type-$date', 'date': date},
    };

void main() {
  group('contentDayKeys', () {
    test('collects activity dates and memory dates, date part only', () {
      expect(
        contentDayKeys(
          [_activity('2026-06-14T09:30:00'), _activity('2026-06-15')],
          [_memory('2026-06-20T18:00:00'), _memory('2026-06-14')],
        ),
        {'2026-06-14', '2026-06-15', '2026-06-20'},
      );
    });

    test('counts every dated item type, not just memories', () {
      // The activity panel turns journal/encounter/segment items into day
      // headers exactly like memories do, so each one pins its day.
      expect(
        contentDayKeys(const [], [
          _item('memory', '2026-07-01'),
          _item('journal', '2026-07-02'),
          _item('encounter', '2026-07-03'),
          _item('segment', '2026-07-04'),
        ]),
        {'2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04'},
      );
    });

    test('ignores missing, null and empty dates', () {
      expect(
        contentDayKeys(
          [
            _activity('2026-06-14'),
            <String, dynamic>{'start_date_local': null},
            <String, dynamic>{},
          ],
          [
            {'item_type': 'memory', 'memory': null},
            {'item_type': 'memory', 'memory': {'date': ''}},
            {'item_type': 'journal', 'journal': {'date': null}},
            {'item_type': 'segment', 'segment': <String, dynamic>{}},
          ],
        ),
        {'2026-06-14'},
      );
    });

    test('an activity item does not need a date of its own', () {
      // Activity items carry only an activity_id; the date lives on the
      // activity itself, which is already covered by the activities list.
      expect(
        contentDayKeys(
          [_activity('2026-06-14')],
          [
            {'item_type': 'activity', 'activity_id': 7},
          ],
        ),
        {'2026-06-14'},
      );
    });
  });

  group('classifyTripEndOrphans', () {
    test('keeps the end date itself and everything before it', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-10', '2026-06-14', '2026-06-15'],
        daysWithContent: const {},
        tripEnd: '2026-06-14',
      );
      expect(o.removable, ['2026-06-15']);
      expect(o.pinned, isEmpty);
    });

    test('days after the end with an activity or memory are pinned, not removable', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-15', '2026-06-16', '2026-06-17'],
        daysWithContent: const {'2026-06-16'},
        tripEnd: '2026-06-14',
      );
      expect(o.removable, ['2026-06-15', '2026-06-17']);
      expect(o.pinned, ['2026-06-16']);
    });

    test('output is sorted and de-duplicated regardless of input order', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-17', '2026-06-15', '2026-06-17', '2026-06-16'],
        daysWithContent: const {},
        tripEnd: '2026-06-14',
      );
      expect(o.removable, ['2026-06-15', '2026-06-16', '2026-06-17']);
    });

    test('content on a day at or before the end date is irrelevant', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-13', '2026-06-14'],
        daysWithContent: const {'2026-06-13', '2026-06-14'},
        tripEnd: '2026-06-14',
      );
      expect(o.removable, isEmpty);
      expect(o.pinned, isEmpty);
    });

    test('crossing a month and a year boundary compares as dates', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-09-30', '2026-10-01', '2027-01-02'],
        daysWithContent: const {},
        tripEnd: '2026-09-30',
      );
      expect(o.removable, ['2026-10-01', '2027-01-02']);
    });

    test('every day after the end date can be pinned, leaving nothing removable', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-15', '2026-06-16'],
        daysWithContent: const {'2026-06-15', '2026-06-16'},
        tripEnd: '2026-06-14',
      );
      expect(o.removable, isEmpty);
      expect(o.pinned, ['2026-06-15', '2026-06-16']);
    });

    test('nothing after the end date leaves both lists empty', () {
      final o = classifyTripEndOrphans(
        dayKeys: ['2026-06-01', '2026-06-02'],
        daysWithContent: const {'2026-06-02'},
        tripEnd: '2026-12-31',
      );
      expect(o.removable, isEmpty);
      expect(o.pinned, isEmpty);
    });
  });

  group('tripEndWarningMessage', () {
    // The wrong sentence here is not cosmetic — see the function's doc.
    test('days that only stay because of another member say so, and do not '
        'ask the user to remove content they cannot see', () {
      final msg = tripEndWarningMessage(
        gone: 0, visibleKept: 0, hiddenKept: 1, when: 'Jun 14, 2026');
      expect(msg, contains('1 day after Jun 14, 2026'));
      expect(msg, contains('other trip'));
      expect(msg, contains('Only they can remove it.'));
      expect(msg, isNot(contains('move or delete that content first')));
    });

    test('days the user can act on keep the actionable wording', () {
      final msg = tripEndWarningMessage(
        gone: 0, visibleKept: 2, hiddenKept: 0, when: 'Jun 14, 2026');
      expect(msg, contains('2 days after Jun 14, 2026'));
      expect(msg, contains('move or delete that content first'));
      expect(msg, isNot(contains('Only they can remove it.')));
    });

    test('a mix names both, in separate sentences', () {
      final msg = tripEndWarningMessage(
        gone: 1, visibleKept: 1, hiddenKept: 2, when: 'Jun 14, 2026');
      final parts = msg.split('\n\n');
      expect(parts, hasLength(3));
      expect(parts[0], contains('1 day after Jun 14, 2026 will be deleted.'));
      expect(parts[1], contains('move or delete that content first'));
      expect(parts[2], contains('2 days'));
      expect(parts[2], contains('Only they can remove it.'));
    });

    test('an unreachable server says offline; a refusal does not', () {
      final offline = tripEndWarningMessage(
        gone: 0, visibleKept: 0, hiddenKept: 1, when: 'Jun 14, 2026',
        failure: ContentCheckFailure.unreachable);
      expect(offline, contains('nothing will be deleted'));
      expect(offline, contains('back online'));

      final refused = tripEndWarningMessage(
        gone: 0, visibleKept: 1, hiddenKept: 0, when: 'Jun 14, 2026',
        failure: ContentCheckFailure.refused);
      expect(refused, contains('nothing will be deleted'));
      expect(refused, isNot(contains('back online')));
      expect(refused, contains('server could not answer'));
    });

    test('a failed check counts every pinned day, however it was classified', () {
      final msg = tripEndWarningMessage(
        gone: 0, visibleKept: 2, hiddenKept: 3, when: 'Jun 14, 2026',
        failure: ContentCheckFailure.unreachable);
      expect(msg, contains('5 days after Jun 14, 2026'));
    });

    test('singular and plural agree', () {
      expect(
        tripEndWarningMessage(
          gone: 1, visibleKept: 0, hiddenKept: 0, when: 'Jun 14, 2026'),
        '1 day after Jun 14, 2026 will be deleted.',
      );
      expect(
        tripEndWarningMessage(
          gone: 3, visibleKept: 0, hiddenKept: 0, when: 'Jun 14, 2026'),
        '3 days after Jun 14, 2026 will be deleted.',
      );
    });
  });
}
