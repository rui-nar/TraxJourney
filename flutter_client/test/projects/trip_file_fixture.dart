/// An in-memory picked trip file for the import tests (issue #469).
library;

import 'package:traxjourney_client/src/projects/upload/trip_file.dart';

/// A picked file holding [bytes], counting how often it is opened.
class MemoryTripFile extends StreamedTripFile {
  MemoryTripFile(this.bytes, {String name = 'trip.traxj'})
      : super(name, bytes.length, () => Stream.value(bytes));

  final List<int> bytes;

  /// How many read streams were opened over this file.
  int opens = 0;

  @override
  Stream<List<int>> openRead() {
    opens++;
    return super.openRead();
  }
}
