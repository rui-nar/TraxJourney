/// Pins the client side of the project-file contract (issue #151): project
/// files are `.traxj`, exported from `/export-traxj`, and the import picker
/// accepts nothing else — project files in the pre-rename formats are
/// deliberately no longer importable.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:cross_file/cross_file.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_file.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

class _FakeProjectsService extends ProjectsService {
  @override
  Future<List<Map<String, dynamic>>> list() async => [];
}

/// Records the extensions the notifier asks the picker to filter on.
class _RecordingFilePickerPlatform extends FilePickerPlatform {
  _RecordingFilePickerPlatform(this.file);
  final PlatformFile? file;
  List<String>? allowedExtensions;

  @override
  Future<PlatformFile?> pickFile({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async {
    this.allowedExtensions = allowedExtensions;
    return file;
  }
}

final class _FakePlatformFile extends PlatformFile {
  _FakePlatformFile(this.name);

  @override
  final String name;

  final Uint8List bytes = utf8.encode('{}');

  @override
  Uri get uri => Uri.dataFromBytes(bytes);

  @override
  XFile get xFile => XFile.fromData(bytes, name: name);

  @override
  Future<int> length() async => bytes.length;

  @override
  Future<Uint8List> readAsBytes() async => bytes;

  @override
  Stream<Uint8List> readAsByteStream() => Stream.value(bytes);
}

void main() {
  test('the project-file extension and export route are traxj', () {
    expect(kProjectFileExtension, 'traxj');
    expect(const ProjectRef(name: 'Trip').path(kProjectFileExportRoute),
        '/api/projects/Trip/export-traxj');
  });

  test('the import picker accepts only .traxj and strips it from the name',
      () async {
    final picker = _RecordingFilePickerPlatform(_FakePlatformFile('Trip.traxj'));
    FilePickerPlatform.instance = picker;

    final picked =
        await ProjectsNotifier(_FakeProjectsService()).pickProjectFile();

    expect(picker.allowedExtensions, ['traxj']);
    expect(picked!.defaultName, 'Trip');
  });

  for (final name in ['Summer.viewtrip', 'Summer.gettracks', 'Summer', '.traxj']) {
    test('a picked "$name" is refused, not imported under that name', () async {
      FilePickerPlatform.instance =
          _RecordingFilePickerPlatform(_FakePlatformFile(name));
      final notifier = ProjectsNotifier(_FakeProjectsService());

      final picked = await notifier.pickProjectFile();

      expect(picked, isNull);
      expect(notifier.error, contains('.traxj'));
    });
  }

  test('an imported project is uploaded as <name>.traxj', () async {
    String? body;
    api = ApiClient(baseUrl: '');
    final notifier = ProjectsNotifier(_FakeProjectsService());

    await http.runWithClient(
      () => notifier.uploadProjectFile(bytes: utf8.encode('{}'), name: 'Trip'),
      () => MockClient((req) async {
        body = req.body;
        return http.Response(jsonEncode({'name': 'Trip'}), 201);
      }),
    );

    expect(body, contains('filename="Trip.traxj"'));
    expect(notifier.error, isNull);
  });
}
