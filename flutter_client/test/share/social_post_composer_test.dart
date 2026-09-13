import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/share/social_post_composer.dart';

void main() {
  group('SocialPostComposer.compose', () {
    test('trims the body text', () {
      final post = SocialPostComposer.compose(
        customText: '  hello world  ',
        includeLink: false,
        link: null,
        selectedPhotoUuids: const [],
        includeMap: false,
      );
      expect(post.text, 'hello world');
    });

    test('appends link with a blank-line separator when included', () {
      final post = SocialPostComposer.compose(
        customText: 'Great day!',
        includeLink: true,
        link: 'https://x/share/t?memory=abc',
        selectedPhotoUuids: const [],
        includeMap: false,
      );
      expect(post.text, 'Great day!\n\nhttps://x/share/t?memory=abc');
      expect(post.link, 'https://x/share/t?memory=abc');
    });

    test('omits link when includeLink is false', () {
      final post = SocialPostComposer.compose(
        customText: 'Body',
        includeLink: false,
        link: 'https://x/share/t?memory=abc',
        selectedPhotoUuids: const [],
        includeMap: false,
      );
      expect(post.text, 'Body');
      expect(post.link, isNull);
    });

    test('omits link when link is null even if includeLink is true', () {
      final post = SocialPostComposer.compose(
        customText: 'Body',
        includeLink: true,
        link: null,
        selectedPhotoUuids: const [],
        includeMap: false,
      );
      expect(post.text, 'Body');
      expect(post.link, isNull);
    });

    test('empty body with link yields the link only', () {
      final post = SocialPostComposer.compose(
        customText: '   ',
        includeLink: true,
        link: 'https://x/l',
        selectedPhotoUuids: const [],
        includeMap: false,
      );
      expect(post.text, 'https://x/l');
    });

    test('passes photo uuids and includeMap through unchanged', () {
      final post = SocialPostComposer.compose(
        customText: 'x',
        includeLink: false,
        link: null,
        selectedPhotoUuids: const ['a', 'b', 'c'],
        includeMap: true,
      );
      expect(post.photoUuids, ['a', 'b', 'c']);
      expect(post.includeMap, isTrue);
    });
  });
}
