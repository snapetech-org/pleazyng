import 'dart:convert';
import 'dart:io';

import 'package:path/path.dart' as p;

const karaokeSnapshotLimit = 32 * 1024 * 1024;

Future<Map<String, dynamic>?> readKaraokeSnapshot(File file) async {
  try {
    if (await FileSystemEntity.type(file.path, followLinks: false) != FileSystemEntityType.file) return null;
    final handle = await file.open();
    try {
      final size = await handle.length();
      if (size > karaokeSnapshotLimit) return null;
      final bytes = await handle.read(size);
      if (bytes.length != size || await handle.readByte() != -1) return null;
      final data = jsonDecode(utf8.decode(bytes));
      return data is Map<String, dynamic> ? data : null;
    } finally {
      await handle.close();
    }
  } on FormatException {
    return null;
  } on FileSystemException {
    return null;
  }
}

/// An interrupted rotation leaves either the current file or a readable backup.
Future<void> writeKaraokeSnapshot(File file, List<int> bytes) async {
  final staging = await file.parent.createTemp('.snapshot-');
  try {
    final pending = File(p.join(staging.path, 'pending'));
    await pending.writeAsBytes(bytes, flush: true);
    if (await readKaraokeSnapshot(file) != null) await file.rename('${file.path}.previous');
    await pending.rename(file.path);
  } finally {
    await staging.delete(recursive: true);
  }
}
