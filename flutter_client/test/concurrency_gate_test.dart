import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/concurrency_gate.dart';

/// Guards the fix for the map-view thumbnail storm: MapPanel fires one HTTP
/// request per memory marker with no virtualization, and an unthrottled
/// burst (100+ requests for a photo-heavy trip) repeatedly crashed the
/// production API. ConcurrencyGate is what caps that fan-out.
void main() {
  test('never runs more than maxConcurrent tasks at once', () async {
    final gate = ConcurrencyGate(3);
    var active = 0;
    var maxActive = 0;
    final completers = List.generate(10, (_) => Completer<void>());

    final futures = [
      for (var i = 0; i < 10; i++)
        gate.run(() async {
          active++;
          maxActive = active > maxActive ? active : maxActive;
          await completers[i].future;
          active--;
        }),
    ];

    // Let every task that can start, start.
    await Future<void>.delayed(Duration.zero);
    expect(active, 3);

    for (final c in completers) {
      c.complete();
      await Future<void>.delayed(Duration.zero);
    }
    await Future.wait(futures);

    expect(maxActive, 3);
  });

  test('queued tasks still all complete, in FIFO order', () async {
    final gate = ConcurrencyGate(1);
    final order = <int>[];

    final futures = [
      for (var i = 0; i < 5; i++)
        gate.run(() async {
          order.add(i);
        }),
    ];
    await Future.wait(futures);

    expect(order, [0, 1, 2, 3, 4]);
  });

  test('propagates the task\'s result and errors', () async {
    final gate = ConcurrencyGate(2);

    expect(await gate.run(() async => 42), 42);
    await expectLater(
      gate.run(() async => throw StateError('boom')),
      throwsA(isA<StateError>()),
    );
  });
}
