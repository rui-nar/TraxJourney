// Unit tests for the first-launch onboarding flag (Android launch UX):
// readHasSeenOnboarding() reflects what's persisted, and markSeen() flips
// the in-memory flag immediately (so the router's Listenable.merge picks it
// up without waiting on the SharedPreferences write) and persists it.

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/onboarding_notifier.dart';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('readHasSeenOnboarding is false when nothing was ever saved',
      () async {
    expect(await readHasSeenOnboarding(), isFalse);
  });

  test('readHasSeenOnboarding reflects a previously saved true', () async {
    SharedPreferences.setMockInitialValues({'has_seen_onboarding': true});
    expect(await readHasSeenOnboarding(), isTrue);
  });

  test('markSeen flips hasSeenOnboarding synchronously and notifies',
      () async {
    final notifier = OnboardingNotifier(false);
    var notified = false;
    notifier.addListener(() => notified = true);

    final future = notifier.markSeen();

    expect(notifier.hasSeenOnboarding, isTrue);
    expect(notified, isTrue);
    await future;
  });

  test('markSeen persists so a later readHasSeenOnboarding sees it',
      () async {
    final notifier = OnboardingNotifier(false);
    await notifier.markSeen();

    expect(await readHasSeenOnboarding(), isTrue);
  });

  test('markSeen is a no-op (no extra notify) when already seen', () async {
    final notifier = OnboardingNotifier(true);
    var notifyCount = 0;
    notifier.addListener(() => notifyCount++);

    await notifier.markSeen();

    expect(notifyCount, 0);
  });
}
