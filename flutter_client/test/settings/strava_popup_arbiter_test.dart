// The closed-versus-message decision for the Strava OAuth popup
// (docs/STRAVA_CONNECT_FOLLOWUPS_PLAN.md, D3, R1-2 and R2-3), on a fake clock.

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/settings/strava_popup_arbiter.dart';

void main() {
  Duration ms(int n) => Duration(milliseconds: n);

  test('a message wins over a closed popup seen 200 ms earlier', () {
    fakeAsync((async) {
      var closed = false;
      final arbiter = PopupResultArbiter<String>(isClosed: () => closed);
      String? got = 'unset';
      var done = false;
      arbiter.result.then((v) {
        got = v;
        done = true;
      });

      closed = true;
      async.elapse(ms(500)); // The check sees `closed`; grace starts.
      async.elapse(ms(200));
      expect(done, isFalse);
      arbiter.onMessage('code');
      async.flushMicrotasks();

      expect(got, 'code');
      expect(async.pendingTimers, isEmpty);
    });
  });

  test('closed with no message in the grace period reports closed', () {
    fakeAsync((async) {
      final arbiter = PopupResultArbiter<String>(isClosed: () => true);
      String? got = 'unset';
      arbiter.result.then((v) => got = v);

      async.elapse(ms(500));
      async.flushMicrotasks();
      expect(got, 'unset'); // Still inside the grace period.
      async.elapse(ms(499));
      async.flushMicrotasks();
      expect(got, 'unset');
      async.elapse(ms(1));
      async.flushMicrotasks();
      expect(got, isNull);
      expect(async.pendingTimers, isEmpty);
    });
  });

  test('an open popup is never reported closed', () {
    fakeAsync((async) {
      final arbiter = PopupResultArbiter<String>(isClosed: () => false);
      var done = false;
      arbiter.result.then((_) => done = true);
      async.elapse(const Duration(minutes: 5));
      async.flushMicrotasks();
      expect(done, isFalse);
      arbiter.cancel();
      expect(async.pendingTimers, isEmpty);
    });
  });

  test('completes exactly once: later messages and closes are ignored', () {
    fakeAsync((async) {
      var closed = false;
      final arbiter = PopupResultArbiter<String>(isClosed: () => closed);
      final results = <String?>[];
      arbiter.result.then(results.add);

      closed = true;
      async.elapse(ms(1000)); // Closed after the grace period.
      async.flushMicrotasks();
      expect(results, [null]);

      arbiter.onMessage('late'); // After the closed result.
      arbiter.onMessage('later');
      async.elapse(const Duration(seconds: 5));
      async.flushMicrotasks();
      expect(results, [null]);
    });

    fakeAsync((async) {
      final arbiter = PopupResultArbiter<String>(isClosed: () => false);
      final results = <String?>[];
      arbiter.result.then(results.add);

      arbiter.onMessage('first');
      arbiter.onMessage('second'); // A second message.
      async.flushMicrotasks();
      expect(results, ['first']);
    });
  });

  test('no timer is left pending after completion', () {
    fakeAsync((async) {
      final arbiter = PopupResultArbiter<String>(isClosed: () => true);
      arbiter.result.then((_) {});
      async.elapse(ms(1000));
      async.flushMicrotasks();
      expect(async.pendingTimers, isEmpty);
    });
  });

  test('a message before any check reports the message with no timers', () {
    fakeAsync((async) {
      final arbiter = PopupResultArbiter<String>(isClosed: () => true);
      String? got;
      arbiter.result.then((v) => got = v);

      arbiter.onMessage('code');
      async.flushMicrotasks();

      expect(got, 'code');
      expect(async.pendingTimers, isEmpty);
    });
  });
}
