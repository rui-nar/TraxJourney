// Unit tests for ProjectNotifier's ProjectRef addressing (issue #106 —
// travel companion; capability getters + caller_role correction: issue
// #109): load() adopts the given ref, projectName/capability getters derive
// from it, a `caller_role` in the load response corrects the role, and a
// rename updates the name in place while keeping the owner/role.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

class _FakeProjectService extends ProjectService {
  /// Set by a test to simulate the server's `caller_role` field; null omits
  /// it entirely (older-server / own-project shape).
  String? callerRole;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
        if (callerRole != null) 'caller_role': callerRole,
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}

void main() {
  group('ProjectNotifier ref addressing', () {
    test('load() with an own ref: projectName set, full capabilities',
        () async {
      final n = _TestProjectNotifier(_FakeProjectService());
      await n.load(const ProjectRef(name: 'Trip'));

      expect(n.projectName, 'Trip');
      expect(n.ref, const ProjectRef(name: 'Trip'));
      expect(n.isProjectOwner, isTrue);
      expect(n.canEditContent, isTrue);
      expect(n.canManageTrip, isTrue);
      expect(n.isViewer, isFalse);
    });

    test('load() with a shared (editor) ref: canEditContent true but not '
        'canManageTrip, ref carries ownerId/role', () async {
      final n = _TestProjectNotifier(_FakeProjectService());
      await n.load(const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));

      expect(n.projectName, 'Trip');
      expect(n.ref, const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));
      expect(n.canEditContent, isTrue);
      expect(n.canManageTrip, isFalse);
      expect(n.isProjectOwner, isFalse);
    });

    test('the server caller_role corrects a wrong placeholder role (issue '
        '#109) — resolveRoleFor guesses editor for any non-own ref, but the '
        'load response is authoritative', () async {
      final service = _FakeProjectService()..callerRole = 'viewer';
      final n = _TestProjectNotifier(service);
      await n.load(const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));

      expect(n.ref, const ProjectRef(name: 'Trip', ownerId: 7, role: 'viewer'));
      expect(n.isViewer, isTrue);
      expect(n.canEditContent, isFalse);
    });

    test('caller_role also corrects to co-owner', () async {
      final service = _FakeProjectService()..callerRole = 'co-owner';
      final n = _TestProjectNotifier(service);
      await n.load(const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));

      expect(n.canEditContent, isTrue);
      expect(n.canManageTrip, isTrue);
      expect(n.isProjectOwner, isFalse);
    });

    test('before any load(), ref/projectName are null and capabilities '
        'default to the most permissive tier', () {
      final n = _TestProjectNotifier(_FakeProjectService());
      expect(n.ref, isNull);
      expect(n.projectName, isNull);
      expect(n.isViewer, isFalse);
      expect(n.canEditContent, isTrue);
      expect(n.canManageTrip, isTrue);
      expect(n.isProjectOwner, isTrue);
    });

    test('clear() resets ref to null', () async {
      final n = _TestProjectNotifier(_FakeProjectService());
      await n.load(const ProjectRef(name: 'Trip'));
      expect(n.ref, isNotNull);

      n.clear();
      expect(n.ref, isNull);
      expect(n.projectName, isNull);
    });
  });
}
