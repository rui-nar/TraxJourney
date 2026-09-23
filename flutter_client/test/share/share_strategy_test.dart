import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/share/share_interfaces.dart';
import 'package:traxjourney_client/src/share/share_strategy.dart';

class _Caps implements ShareCapabilities {
  @override
  final bool canShareFiles;
  const _Caps(this.canShareFiles);
}

void main() {
  group('ShareStrategy.resolve', () {
    test('system + capable → sheetWithFiles', () {
      expect(
        ShareStrategy.resolve(ShareTarget.system, const _Caps(true)),
        ShareMethod.sheetWithFiles,
      );
    });

    test('system + incapable → sheetTextOnly', () {
      expect(
        ShareStrategy.resolve(ShareTarget.system, const _Caps(false)),
        ShareMethod.sheetTextOnly,
      );
    });

    test('whatsapp → urlIntent regardless of capability', () {
      expect(
        ShareStrategy.resolve(ShareTarget.whatsapp, const _Caps(true)),
        ShareMethod.urlIntent,
      );
      expect(
        ShareStrategy.resolve(ShareTarget.whatsapp, const _Caps(false)),
        ShareMethod.urlIntent,
      );
    });

    test('facebook → urlIntent regardless of capability', () {
      expect(
        ShareStrategy.resolve(ShareTarget.facebook, const _Caps(true)),
        ShareMethod.urlIntent,
      );
      expect(
        ShareStrategy.resolve(ShareTarget.facebook, const _Caps(false)),
        ShareMethod.urlIntent,
      );
    });

    test('copyLink → clipboard regardless of capability', () {
      expect(
        ShareStrategy.resolve(ShareTarget.copyLink, const _Caps(true)),
        ShareMethod.clipboard,
      );
      expect(
        ShareStrategy.resolve(ShareTarget.copyLink, const _Caps(false)),
        ShareMethod.clipboard,
      );
    });
  });
}
