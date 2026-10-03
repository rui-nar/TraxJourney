/// Importing a trip file streams it instead of holding it in memory (#469).
///
/// A trip archive can be 1 GB. The picked file stays a handle (name, size, a
/// way to read it) all the way to the uploader: the notifier never reads it,
/// a retry after a name conflict sends it again from the handle, and a file
/// over its type's limit is refused before anything is sent.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:cross_file/cross_file.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';
import 'package:traxjourney_client/src/projects/upload/trip_file.dart';

import 'trip_file_fixture.dart';

class _FakeProjectsService extends ProjectsService {
  @override
  Future<List<Map<String, dynamic>>> list() async => [];
}

/// What the notifier handed the uploader.
typedef _Sent = ({
  Uri url,
  Map<String, String> headers,
  String field,
  String filename,
  TripFile file,
});

/// Records every upload and answers with the next of [answers].
class _FakeUploader implements TripFileUploader {
  _FakeUploader(this.answers);

  final List<TripFileUploadResponse> answers;
  final sent = <_Sent>[];

  @override
  Future<TripFileUploadResponse> send({
    required Uri url,
    required Map<String, String> headers,
    required String field,
    required String filename,
    required TripFile file,
  }) async {
    sent.add((
      url: url,
      headers: headers,
      field: field,
      filename: filename,
      file: file,
    ));
    return answers.removeAt(0);
  }
}

TripFileUploadResponse _created(String name) =>
    (statusCode: 201, body: jsonEncode({'name': name, 'outcome': 'created'}));

/// A picked file of [size] bytes that fails the test if anything reads it.
final class _UnreadablePlatformFile extends PlatformFile {
  _UnreadablePlatformFile(this.name, this.size);

  @override
  final String name;
  final int size;

  @override
  Uri get uri => Uri.file('/picked/$name');

  @override
  XFile get xFile => fail('the picked file was read');

  @override
  Future<int> length() async => size;

  @override
  Future<Uint8List> readAsBytes() => fail('the picked file was read');

  @override
  Stream<Uint8List> readAsByteStream() => fail('the picked file was read');
}

class _FilePickerPlatform extends FilePickerPlatform {
  _FilePickerPlatform(this.file);
  final PlatformFile file;

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
  }) async =>
      file;
}

const _gb = 1024 * 1024 * 1024;
const _mb50 = 50 * 1024 * 1024;

void main() {
  setUp(() => api = ApiClient(baseUrl: '')..setToken('tok'));

  group('the notifier', () {
    test('hands the uploader the picked handle and its length, unread',
        () async {
      final uploader = _FakeUploader([_created('Alps')]);
      final notifier =
          ProjectsNotifier(_FakeProjectsService(), uploader: uploader);
      final file = MemoryTripFile([1, 2, 3], name: 'picked.zip');

      final saved = await notifier.uploadProjectFile(
          file: file, name: 'Alps', extension: 'zip');

      expect(saved, 'Alps');
      final sent = uploader.sent.single;
      expect(sent.file, same(file));
      expect(sent.file.size, 3);
      expect(file.opens, 0, reason: 'the notifier must not read the file');
      expect(sent.url.path, '/api/projects/import-zip');
      expect(sent.field, 'file');
      expect(sent.filename, 'Alps.zip');
      expect(sent.headers, {'Authorization': 'Bearer tok'});
    });

    test('re-sends the same handle when retrying a name conflict', () async {
      final uploader = _FakeUploader([
        (
          statusCode: 409,
          body: jsonEncode({
            'detail': 'You already have a trip with this name.',
            'code': 'name_conflict',
            'name': 'Alps',
          }),
        ),
        _created('Alps (2)'),
      ]);
      final notifier =
          ProjectsNotifier(_FakeProjectsService(), uploader: uploader);
      final file = MemoryTripFile([1, 2, 3], name: 'picked.zip');

      expect(
          await notifier.uploadProjectFile(
              file: file, name: 'Alps', extension: 'zip'),
          isNull);
      expect(notifier.nameConflict, 'Alps');
      final saved = await notifier.uploadProjectFile(
          file: file,
          name: 'Alps',
          extension: 'zip',
          onConflict: ImportConflictChoice.keepBoth);

      expect(saved, 'Alps (2)');
      expect(uploader.sent.map((s) => s.file), [same(file), same(file)]);
      expect(uploader.sent.last.url.queryParameters['on_conflict'], 'copy');
      expect(file.opens, 0);
    });

    test("maps the uploader's error answer as before", () async {
      final uploader = _FakeUploader([(statusCode: 413, body: '<html>')]);
      final notifier =
          ProjectsNotifier(_FakeProjectsService(), uploader: uploader);

      await notifier.uploadProjectFile(
          file: MemoryTripFile([1]), name: 'Alps', extension: 'zip');

      expect(notifier.error,
          'This file is too large to import. The limit is 1 GB.');
      expect(notifier.isLoading, isFalse);
    });
  });

  group('the native uploader', () {
    test('streams a fresh read of the file, with its length, on every send',
        () async {
      final file = MemoryTripFile(utf8.encode('{"trip": 1}'));
      final notifier = ProjectsNotifier(_FakeProjectsService());
      final posts = <http.MultipartRequest>[];
      final bodies = <String>[];

      await http.runWithClient(
        () async {
          await notifier.uploadProjectFile(file: file, name: 'Alps');
          await notifier.uploadProjectFile(
              file: file,
              name: 'Alps',
              onConflict: ImportConflictChoice.replace);
        },
        () => MockClient.streaming((req, body) async {
          if (req.method != 'POST') {
            return http.StreamedResponse(Stream.value(utf8.encode('[]')), 200);
          }
          posts.add(req as http.MultipartRequest);
          bodies.add(utf8.decode(await body.toBytes()));
          return http.StreamedResponse(
              Stream.value(utf8.encode(jsonEncode({'name': 'Alps'}))), 201);
        }),
      );

      expect(posts, hasLength(2));
      for (final post in posts) {
        expect(post.files.single.length, file.size);
        expect(post.contentLength, isNotNull);
        expect(post.headers['Authorization'], 'Bearer tok');
      }
      for (final body in bodies) {
        expect(body, contains('filename="Alps.traxj"'));
        expect(body, contains('{"trip": 1}'));
      }
      expect(file.opens, 2, reason: 'a retry must re-open the file');
      expect(notifier.error, isNull);
    });
  });

  group('picking', () {
    for (final (name, limit, message) in [
      ('Alps.zip', _gb, 'This file is too large to import. The limit is 1 GB.'),
      (
        'Alps.traxj',
        _mb50,
        'This file is too large to import. The limit is 50 MB.'
      ),
    ]) {
      test('$name over its limit is refused unread, before any upload',
          () async {
        FilePickerPlatform.instance =
            _FilePickerPlatform(_UnreadablePlatformFile(name, limit + 1));
        final uploader = _FakeUploader([]);
        final notifier =
            ProjectsNotifier(_FakeProjectsService(), uploader: uploader);

        final picked = await notifier.pickProjectFile();

        expect(picked, isNull);
        expect(notifier.error, message);
        expect(uploader.sent, isEmpty);
      });

      test('$name at its limit is picked as a handle, unread', () async {
        FilePickerPlatform.instance =
            _FilePickerPlatform(_UnreadablePlatformFile(name, limit));
        final notifier = ProjectsNotifier(_FakeProjectsService());

        final picked = await notifier.pickProjectFile();

        expect(picked!.file.size, limit);
        expect(picked.file.name, name);
        expect(picked.defaultName, 'Alps');
        expect(notifier.error, isNull);
      });
    }
  });
}
