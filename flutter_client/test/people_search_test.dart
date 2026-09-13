import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/people_search.dart';

Map<String, dynamic> _p(int id, {String? name, String? email, String? phone,
    String? polarsteps, String? notes}) => {
      'id': id, 'name': name, 'email': email, 'phone': phone,
      'polarsteps': polarsteps, 'notes': notes,
    };

Map<String, dynamic> _enc(int personId, {String? note}) => {
      'item_type': 'encounter',
      'encounter': {'person_id': personId, 'description': note},
    };

Map<String, dynamic> _groupEnc(int groupId, {String? note, String? date}) => {
      'item_type': 'encounter',
      'encounter': {'group_id': groupId, 'description': note, 'date': date},
    };

void main() {
  group('personDisplayName', () {
    test('falls back to Unknown when name is missing or blank', () {
      expect(personDisplayName(_p(1)), 'Unknown');
      expect(personDisplayName(_p(1, name: '  ')), 'Unknown');
      expect(personDisplayName(_p(1, name: 'Alice')), 'Alice');
    });
  });

  group('groups (#50)', () {
    test('groupDisplayName falls back to "Group" when unnamed', () {
      expect(groupDisplayName({'id': 1}), 'Group');
      expect(groupDisplayName({'id': 1, 'name': '  '}), 'Group');
      expect(groupDisplayName({'id': 1, 'name': 'Crew'}), 'Crew');
    });

    test('memberCountByGroup + membersOfGroup partition people by group_id', () {
      final people = [
        {'id': 1, 'name': 'A', 'group_id': 10},
        {'id': 2, 'name': 'B', 'group_id': 10},
        {'id': 3, 'name': 'C', 'group_id': 20},
        {'id': 4, 'name': 'D', 'group_id': null},
      ];
      expect(memberCountByGroup(people), {10: 2, 20: 1});
      expect(membersOfGroup(people, 10).map((p) => p['id']), [1, 2]);
      expect(membersOfGroup(people, 99), isEmpty);
    });

    test('classifyEncounterPin masks grouped people behind their group', () {
      final peopleById = {
        1: {'id': 1, 'name': 'A', 'group_id': 10},
        2: {'id': 2, 'name': 'B'}, // ungrouped
      };
      final groupsById = {
        10: {'id': 10, 'name': 'Crew'},
      };
      // Grouped person → group pin.
      final g = classifyEncounterPin(1, null, peopleById, groupsById)!;
      expect(g.kind, 'group');
      expect(g.entity['name'], 'Crew');
      // Ungrouped person → person pin.
      final p = classifyEncounterPin(2, null, peopleById, groupsById)!;
      expect(p.kind, 'person');
      expect(p.entity['name'], 'B');
      // Unknown person → null.
      expect(classifyEncounterPin(99, null, peopleById, groupsById), isNull);
      // Grouped but the group is missing → fall back to person.
      final orphan = classifyEncounterPin(1, null, peopleById, {})!;
      expect(orphan.kind, 'person');
    });

    test('classifyEncounterPin resolves a direct group_id without needing a person (#56)', () {
      final groupsById = {10: {'id': 10, 'name': 'Crew'}};
      final g = classifyEncounterPin(null, 10, {}, groupsById)!;
      expect(g.kind, 'group');
      expect(g.entity['name'], 'Crew');
      // group_id set but the group is missing → orphan, no person fallback.
      expect(classifyEncounterPin(null, 99, {}, groupsById), isNull);
      // group_id takes priority even when a person_id is also present.
      final peopleById = {1: {'id': 1, 'name': 'A'}};
      final both = classifyEncounterPin(1, 10, peopleById, groupsById)!;
      expect(both.kind, 'group');
    });
  });

  group('encounter aggregation', () {
    test('counts and collects notes per person', () {
      final items = [
        _enc(1, note: 'cafe'),
        _enc(1, note: 'beach'),
        _enc(2, note: 'trail'),
        {'item_type': 'activity'},
      ];
      final people = [_p(1), _p(2)];
      expect(encounterCountByPerson(items, people), {1: 2, 2: 1});
      expect(encounterNotesByPerson(items, people),
          {1: ['cafe', 'beach'], 2: ['trail']});
    });

    test('members inherit their group\'s encounters (#124)', () {
      final people = [
        {'id': 1, 'name': 'A', 'group_id': 10},
        {'id': 2, 'name': 'B', 'group_id': 10},
        {'id': 3, 'name': 'C'}, // ungrouped
      ];
      final items = [
        _enc(1, note: 'cafe'),
        _groupEnc(10, note: 'met the crew', date: '2024-06-02'),
        _groupEnc(20, note: 'other crew', date: '2024-06-03'),
        _enc(3, note: 'trail'),
      ];
      // Both members pick up the group encounter; the group-20 one belongs to
      // nobody here, and the ungrouped person is unaffected.
      expect(encounterCountByPerson(items, people), {1: 2, 2: 1, 3: 1});
      expect(encounterNotesByPerson(items, people), {
        1: ['cafe', 'met the crew'],
        2: ['met the crew'],
        3: ['trail'],
      });
      expect(encountersByPerson(items, people)[2]!.single['date'], '2024-06-02');
    });

    test('inheritedGroupLabel only labels group-sourced encounters (#124)', () {
      expect(inheritedGroupLabel({'source': 'person', 'group_name': 'Crew'}),
          isNull);
      expect(inheritedGroupLabel({'description': 'cafe'}), isNull);
      expect(inheritedGroupLabel({'source': 'group', 'group_name': 'Crew'}),
          'With Crew');
      // Unnamed group → generic wording, never a blank "With ".
      expect(inheritedGroupLabel({'source': 'group', 'group_name': '  '}),
          'With the group');
      expect(inheritedGroupLabel({'source': 'group'}), 'With the group');
    });

    test('counts, collects notes, and lists encounters per group (#56)', () {
      final items = [
        _groupEnc(10, note: 'met the crew', date: '2024-06-01'),
        _groupEnc(10, note: 'again', date: '2024-06-02'),
        _groupEnc(20, note: 'other crew', date: '2024-06-03'),
        _enc(1, note: 'unrelated person encounter'),
        {'item_type': 'activity'},
      ];
      expect(encounterCountByGroup(items), {10: 2, 20: 1});
      expect(encounterNotesByGroup(items),
          {10: ['met the crew', 'again'], 20: ['other crew']});
      final forTen = encountersForGroup(items, 10);
      expect(forTen.map((e) => e['date']), ['2024-06-01', '2024-06-02']);
      expect(encountersForGroup(items, 99), isEmpty);
    });
  });

  group('filterPeople', () {
    final people = [
      _p(1, name: 'Alice', email: 'alice@x.com', polarsteps: 'al_ps'),
      _p(2, name: 'Bob', phone: '555-123', notes: 'guide in Nepal'),
      _p(3), // Unknown
    ];

    test('blank query returns everyone', () {
      expect(filterPeople(people, '   ', {}).length, 3);
    });

    test('matches name case-insensitively', () {
      final r = filterPeople(people, 'ALI', {});
      expect(r.map((p) => p['id']), [1]);
    });

    test('matches email, phone, polarsteps and notes fields', () {
      expect(filterPeople(people, 'x.com', {}).map((p) => p['id']), [1]);
      expect(filterPeople(people, '555', {}).map((p) => p['id']), [2]);
      expect(filterPeople(people, 'al_ps', {}).map((p) => p['id']), [1]);
      expect(filterPeople(people, 'nepal', {}).map((p) => p['id']), [2]);
    });

    test('matches on a person\'s encounter notes', () {
      final notes = {3: ['met at the summit hut']};
      final r = filterPeople(people, 'summit', notes);
      expect(r.map((p) => p['id']), [3]);
    });

    test('no match returns empty', () {
      expect(filterPeople(people, 'zzz', {}), isEmpty);
    });
  });
}
