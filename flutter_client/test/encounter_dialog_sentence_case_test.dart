import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/encounter_dialog.dart';

/// Guards issue #77: the encounter note box should start with a capital
/// letter and capitalize the first letter after every sentence-ending
/// '. ' — without touching letters that aren't at a sentence start, and
/// idempotently (safe to reapply on every keystroke/paste).
void main() {
  group('sentenceCase', () {
    test('empty string is unchanged', () {
      expect(sentenceCase(''), '');
    });

    test('capitalizes the first letter of a fresh note', () {
      expect(sentenceCase('hello'), 'Hello');
    });

    test('capitalizes after ". "', () {
      expect(sentenceCase('hello. world'), 'Hello. World');
    });

    test('already-correctly-capitalized text is unchanged', () {
      expect(sentenceCase('Hello. World.'), 'Hello. World.');
    });

    test('multiple sentences typed in sequence', () {
      expect(
        sentenceCase('first sentence. second sentence. third one'),
        'First sentence. Second sentence. Third one',
      );
    });

    test('does not capitalize letters that are not at a sentence start', () {
      // Only the leading "h" is capitalized — "world" stays lowercase.
      expect(sentenceCase('hello world'), 'Hello world');
    });

    test('is idempotent — reapplying does not change already-correct text', () {
      const once = 'hello. there. friend';
      final twice = sentenceCase(sentenceCase(once));
      expect(sentenceCase(once), twice);
    });

    test('a period not followed by whitespace does not trigger a capital', () {
      expect(sentenceCase('version 3.14 released'), 'Version 3.14 released');
    });
  });
}
