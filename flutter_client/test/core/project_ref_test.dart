// Unit tests for ProjectRef (issue #106 — travel companion): URL building
// (?owner= injection), route params, JSON round-trip, and the
// ProjectListEntry extension that reads role/owner_id/owner_name off a raw
// `/api/projects/` list entry — including backward compatibility with an
// older server that doesn't send those fields at all.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';

void main() {
  group('ProjectRef.isOwn / capability getters', () {
    test('an ownerId-null ref is own, regardless of role', () {
      const ref = ProjectRef(name: 'Trip');
      expect(ref.isOwn, isTrue);
      expect(ref.isOwner, isTrue); // role defaults to "owner"
      expect(ref.canManageTrip, isTrue);
      expect(ref.canEditContent, isTrue);
      expect(ref.isViewer, isFalse);
    });

    test('a ref with ownerId set is not own', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');
      expect(ref.isOwn, isFalse);
      expect(ref.isOwner, isFalse);
      expect(ref.canEditContent, isTrue);
      expect(ref.canManageTrip, isFalse);
    });

    test('viewer: read-only — no edit or manage capability', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'viewer');
      expect(ref.isViewer, isTrue);
      expect(ref.canEditContent, isFalse);
      expect(ref.canManageTrip, isFalse);
      expect(ref.isOwner, isFalse);
    });

    test('co-owner: can edit content and manage the trip, but is not owner',
        () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'co-owner');
      expect(ref.isViewer, isFalse);
      expect(ref.canEditContent, isTrue);
      expect(ref.canManageTrip, isTrue);
      expect(ref.isOwner, isFalse);
    });

    test('an unrecognized role string is treated as the lowest tier', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'bogus');
      expect(ref.canEditContent, isFalse);
      expect(ref.canManageTrip, isFalse);
      expect(ref.isOwner, isFalse);
    });
  });

  group('ProjectRef.withOwner', () {
    test('own project: URL is returned unchanged', () {
      const ref = ProjectRef(name: 'Trip');
      expect(ref.withOwner('/api/projects/Trip'), '/api/projects/Trip');
    });

    test('shared project, no existing query: appends ?owner=<id>', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');
      expect(ref.withOwner('/api/projects/Trip'), '/api/projects/Trip?owner=7');
    });

    test('shared project, existing query: appends &owner=<id>', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'editor');
      expect(ref.withOwner('/api/projects/Trip/stats?tags=x'),
          '/api/projects/Trip/stats?tags=x&owner=7');
    });
  });

  group('ProjectRef.path', () {
    test('own project: no owner query, name is URL-encoded', () {
      const ref = ProjectRef(name: 'Trip A');
      expect(ref.path(), '/api/projects/Trip%20A');
      expect(ref.path('/meta'), '/api/projects/Trip%20A/meta');
    });

    test('shared project: appends ?owner=<id> after the suffix', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 42, role: 'editor');
      expect(ref.path('/segments'), '/api/projects/Trip/segments?owner=42');
    });

    test('shared project with a suffix that already has a query string', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 42, role: 'editor');
      expect(ref.path('/stats?tags=norway'),
          '/api/projects/Trip/stats?tags=norway&owner=42');
    });
  });

  group('ProjectRef.routeParams', () {
    test('own project: only project param', () {
      const ref = ProjectRef(name: 'Trip');
      expect(ref.routeParams, {'project': 'Trip'});
    });

    test('shared project: project + owner params', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 42, role: 'editor');
      expect(ref.routeParams, {'project': 'Trip', 'owner': '42'});
    });
  });

  group('ProjectRef JSON round-trip', () {
    test('own project', () {
      const ref = ProjectRef(name: 'Trip');
      expect(ProjectRef.fromJson(ref.toJson()), ref);
    });

    test('shared project (owner + role preserved)', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 42, role: 'editor');
      expect(ProjectRef.fromJson(ref.toJson()), ref);
    });

    test('fromJson defaults role to "owner" when absent', () {
      final ref = ProjectRef.fromJson({'name': 'Trip'});
      expect(ref.role, 'owner');
      expect(ref.ownerId, isNull);
    });
  });

  group('ProjectRef equality', () {
    test('two refs with the same fields are equal', () {
      expect(const ProjectRef(name: 'Trip', ownerId: 1, role: 'editor'),
          const ProjectRef(name: 'Trip', ownerId: 1, role: 'editor'));
    });

    test('differing ownerId makes refs unequal', () {
      expect(const ProjectRef(name: 'Trip', ownerId: 1),
          isNot(const ProjectRef(name: 'Trip', ownerId: 2)));
    });
  });

  group('ProjectListEntry (raw /api/projects/ list entry parsing)', () {
    test('parses owner_id/owner_name/role from a full entry', () {
      final entry = {
        'name': 'Trip',
        'filename': 'trip.traxj',
        'owner_id': 7,
        'owner_name': 'Alice',
        'role': 'editor',
      };
      expect(entry.ref, const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));
      expect(entry.ownerName, 'Alice');
      expect(entry.isSharedWithMe, isTrue);
    });

    test('an owned entry (role: owner) is not "shared with me"', () {
      final entry = {'name': 'Trip', 'owner_id': null, 'role': 'owner'};
      expect(entry.ref, const ProjectRef(name: 'Trip'));
      expect(entry.isSharedWithMe, isFalse);
    });

    test('backward compat: older server with no owner_id/owner_name/role '
        'defaults to an own, role-owner ref', () {
      final entry = {'name': 'Trip', 'filename': 'trip.traxj'};
      expect(entry.ref, const ProjectRef(name: 'Trip'));
      expect(entry.ownerName, isNull);
      expect(entry.isSharedWithMe, isFalse);
    });

    // Regression (issue #109): isSharedWithMe used to check `role == 'editor'`,
    // which miscategorized viewer/co-owner entries as "My Trips" instead of
    // "Shared With Me" once those roles existed.
    test('a viewer entry is "shared with me"', () {
      final entry = {'name': 'Trip', 'owner_id': 7, 'role': 'viewer'};
      expect(entry.isSharedWithMe, isTrue);
    });

    test('a co-owner entry is "shared with me"', () {
      final entry = {'name': 'Trip', 'owner_id': 7, 'role': 'co-owner'};
      expect(entry.isSharedWithMe, isTrue);
    });
  });
}
