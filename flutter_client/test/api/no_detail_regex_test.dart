/// No screen or notifier reads the server's `detail` with a regex (issue #480).
///
/// A regex stopping at the first quote cut messages short and lost validation
/// errors; `apiErrorMessage` / `apiErrorReason` in api/client.dart parse the
/// body as JSON instead. This keeps the regex from coming back.
library;

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('no RegExp matches "detail" anywhere under lib/', () {
    final offenders = <String>[];
    for (final file in Directory('lib').listSync(recursive: true)) {
      if (file is! File || !file.path.endsWith('.dart')) continue;
      final lines = file.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].contains('RegExp(') && lines[i].contains('detail')) {
          offenders.add('${file.path}:${i + 1}: ${lines[i].trim()}');
        }
      }
    }
    expect(offenders, isEmpty,
        reason: 'Use apiErrorMessage or apiErrorReason (api/client.dart) '
            'instead of a regex on "detail".');
  });
}
