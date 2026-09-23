// Unit tests for ProjectRef.resolveRoleFor (issue #106) — URL-derived refs
// carry no role, so screens resolve it against the signed-in user before
// ProjectNotifier.load() — and for clearLastOpenedProject (leave-trip flow).

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/last_opened_project.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';

void main() {
  group('ProjectRef.resolveRoleFor', () {
    test('no ownerId → owner (own project)', () {
      const ref = ProjectRef(name: 'Trip');
      expect(ref.resolveRoleFor('1').role, 'owner');
      expect(ref.resolveRoleFor('1').isOwner, isTrue);
    });

    test("ownerId equal to the caller's id → owner (own list entries carry "
        'their owner_id)', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 1);
      expect(ref.resolveRoleFor('1').role, 'owner');
    });

    test("someone else's ownerId → editor", () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7);
      final resolved = ref.resolveRoleFor('1');
      expect(resolved.role, 'editor');
      expect(resolved.canEditContent, isTrue);
      expect(resolved.canManageTrip, isFalse);
      // Addressing fields are untouched.
      expect(resolved.name, 'Trip');
      expect(resolved.ownerId, 7);
    });

    test('a wrong pre-set role is corrected', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7, role: 'owner');
      expect(ref.resolveRoleFor('1').role, 'editor');
      const own = ProjectRef(name: 'Trip', role: 'editor');
      expect(own.resolveRoleFor('1').role, 'owner');
    });

    test('null selfId with an ownerId resolves to editor (defensive)', () {
      const ref = ProjectRef(name: 'Trip', ownerId: 7);
      expect(ref.resolveRoleFor(null).role, 'editor');
    });
  });

  group('clearLastOpenedProject', () {
    setUp(() => SharedPreferences.setMockInitialValues({}));

    test('forgets the recorded project for that user only', () async {
      await saveLastOpenedProject('u1', const ProjectRef(name: 'Trip A'));
      await saveLastOpenedProject('u2', const ProjectRef(name: 'Trip B'));

      await clearLastOpenedProject('u1');

      expect(await readLastOpenedProject('u1'), isNull);
      expect((await readLastOpenedProject('u2'))?.name, 'Trip B');
    });

    test('no-op for a null user id', () async {
      await saveLastOpenedProject('u1', const ProjectRef(name: 'Trip A'));
      await clearLastOpenedProject(null);
      expect((await readLastOpenedProject('u1'))?.name, 'Trip A');
    });
  });
}
