// Unit tests for the "last opened project" shared_preferences helper
// (issue #93): scoped per-user, write-then-read round trip, and the
// null-userId no-op guard used when auth hasn't resolved yet.
//
// Issue #106 (travel companion) extended this to store a ProjectRef
// (name + ownerId) instead of a bare name, so a shared project's owner
// survives a reload — with backward compatibility for the pre-#106 format,
// which stored the bare project name as a plain (non-JSON) string.

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/last_opened_project.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('saveLastOpenedProject then readLastOpenedProject round-trips', () async {
    await saveLastOpenedProject('user-1', const ProjectRef(name: 'Trip A'));
    expect(await readLastOpenedProject('user-1'), const ProjectRef(name: 'Trip A'));
  });

  test('round-trips a shared project ref (owner + role — issue #106)', () async {
    const ref = ProjectRef(name: 'Trip A', ownerId: 42, role: 'editor');
    await saveLastOpenedProject('user-1', ref);
    expect(await readLastOpenedProject('user-1'), ref);
  });

  test('is scoped per-user — user-2 has no value after user-1 writes', () async {
    await saveLastOpenedProject('user-1', const ProjectRef(name: 'Trip A'));
    expect(await readLastOpenedProject('user-2'), isNull);
  });

  test('readLastOpenedProject returns null when nothing was ever saved', () async {
    expect(await readLastOpenedProject('user-1'), isNull);
  });

  test('saveLastOpenedProject is a no-op when userId is null', () async {
    await saveLastOpenedProject(null, const ProjectRef(name: 'Trip A'));
    expect(await readLastOpenedProject('user-1'), isNull);
  });

  test('readLastOpenedProject returns null when userId is null', () async {
    await saveLastOpenedProject('user-1', const ProjectRef(name: 'Trip A'));
    expect(await readLastOpenedProject(null), isNull);
  });

  test('a later save overwrites the earlier value for the same user', () async {
    await saveLastOpenedProject('user-1', const ProjectRef(name: 'Trip A'));
    await saveLastOpenedProject('user-1', const ProjectRef(name: 'Trip B'));
    expect(await readLastOpenedProject('user-1'), const ProjectRef(name: 'Trip B'));
  });

  test('reads a pre-#106 plain-string value as an own (ownerId-null) ref', () async {
    SharedPreferences.setMockInitialValues({
      'last_opened_project_user-1': 'Legacy Trip',
    });
    expect(await readLastOpenedProject('user-1'), const ProjectRef(name: 'Legacy Trip'));
  });

  test('rootRedirectTarget appends ?owner= for a shared project', () async {
    await saveLastOpenedProject(
        'user-1', const ProjectRef(name: 'Trip A', ownerId: 42, role: 'editor'));
    expect(await rootRedirectTarget('user-1'), '/view?project=Trip%20A&owner=42');
  });
}
