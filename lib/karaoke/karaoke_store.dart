import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import '../utils/serial_future_queue.dart';
import 'karaoke_snapshot.dart';

/// Profile isolation and serialized, recoverable snapshots. No provider tokens.
class KaraokeStore {
  KaraokeStore(this.profileId);
  final String profileId;
  Directory? _directory;
  static final _writes = SerialFutureQueue();

  Future<Directory> get directory async => _directory ??= await _open();

  Future<Directory> _open() async {
    final root = await getApplicationSupportDirectory();
    final key = sha256.convert(utf8.encode(profileId)).toString();
    return Directory(p.join(root.path, 'karaoke', key)).create(recursive: true);
  }

  Future<Map<String, dynamic>?> read(String name) async {
    _validateName(name);
    final dir = await directory;
    for (final suffix in ['', '.previous']) {
      final data = await readKaraokeSnapshot(File(p.join(dir.path, '$name.json$suffix')));
      if (data != null) return data;
    }
    return null;
  }

  Future<void> write(String name, Map<String, dynamic> value) {
    _validateName(name);
    final bytes = utf8.encode(jsonEncode(value));
    if (bytes.length > karaokeSnapshotLimit) throw const FormatException('Karaoke state limit exceeded');
    return _writes.run(() async {
      final dir = await directory;
      await writeKaraokeSnapshot(File(p.join(dir.path, '$name.json')), bytes);
    });
  }

  static void _validateName(String name) {
    if (!RegExp(r'^[a-zA-Z0-9_-]{1,128}$').hasMatch(name)) {
      throw const FormatException('Invalid state name');
    }
  }

  Future<File> asset(String id, String relative) async {
    _validateName(id);
    final base = p.join((await directory).path, 'packages', id);
    final target = p.normalize(p.join(base, relative));
    if (!p.isWithin(base, target) || relative.contains('\\') || p.isAbsolute(relative)) {
      throw const FormatException('Invalid asset path');
    }
    // Imports materialize regular files. Never follow links supplied by a package.
    var probe = target;
    while (probe == base || p.isWithin(base, probe)) {
      if (await FileSystemEntity.type(probe, followLinks: false) == FileSystemEntityType.link) {
        throw const FormatException('Karaoke asset is a symbolic link');
      }
      probe = p.dirname(probe);
    }
    return File(target);
  }
}
