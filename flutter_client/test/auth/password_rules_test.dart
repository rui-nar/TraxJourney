import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/auth/password_rules.dart';

void main() {
  test('accepts a valid change', () {
    expect(
        changePasswordError(current: 'old', next: 'newpw', confirm: 'newpw'),
        isNull);
  });

  test('rejects empty fields', () {
    expect(changePasswordError(current: '', next: 'x', confirm: 'x'),
        'Fill in all fields.');
    expect(changePasswordError(current: 'old', next: '', confirm: ''),
        'Fill in all fields.');
  });

  test('rejects a confirmation mismatch', () {
    expect(changePasswordError(current: 'old', next: 'a', confirm: 'b'),
        'New passwords do not match.');
  });
}
