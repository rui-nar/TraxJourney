/// Unit tests for [PickedFileBytes.readAsBytesOrNull].
///
/// file_picker 12 reads bytes lazily and throws when a file cannot be read,
/// where 11 reported null bytes. Every picker call site treats null as "skip
/// this file", so the shim has to keep swallowing the failure — otherwise the
/// exception escapes an async picker callback with nothing to catch it.
library;

import 'dart:typed_data';

import 'package:cross_file/cross_file.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/picked_file_bytes.dart';

final Uint8List _bytes = Uint8List.fromList([1, 2, 3]);

final class _FakePlatformFile extends PlatformFile {
  _FakePlatformFile({this.unreadable = false});

  final bool unreadable;

  @override
  String get name => 'file.bin';

  @override
  Uri get uri => Uri.dataFromBytes(_bytes);

  @override
  XFile get xFile => XFile.fromData(_bytes, name: name);

  @override
  Future<int> length() async => _bytes.length;

  @override
  Future<Uint8List> readAsBytes() async {
    if (unreadable) throw Exception('file is gone');
    return _bytes;
  }

  @override
  Stream<Uint8List> readAsByteStream() => Stream.value(_bytes);
}

void main() {
  test('returns the bytes when the file reads', () async {
    expect(await _FakePlatformFile().readAsBytesOrNull(), _bytes);
  });

  test('returns null rather than throwing when the file cannot be read',
      () async {
    expect(await _FakePlatformFile(unreadable: true).readAsBytesOrNull(),
        isNull);
  });
}
