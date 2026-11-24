import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;
import 'package:plezy/karaoke/karaoke_store.dart';

class _FileStore extends KaraokeStore {
  _FileStore(this.root) : super('profile');
  final Directory root;
  @override
  Future<Directory> get directory async => root;
}

void main() {
  late Directory root;
  late KaraokeStore store;
  setUp(() async {
    root = await Directory.systemTemp.createTemp('karaoke-store-');
    store = _FileStore(root);
    addTearDown(() => root.delete(recursive: true));
  });

  test('saving after recovery retains the last readable backup', () async {
    await store.write('session', {'turn': 1});
    await store.write('session', {'turn': 2});
    final primary = File(p.join(root.path, 'session.json'));
    await primary.writeAsString('{');
    expect(await store.read('session'), {'turn': 1});
    await store.write('session', {'turn': 3});
    expect(await store.read('session'), {'turn': 3});
    await primary.delete();
    expect(await store.read('session'), {'turn': 1});
  });

  test('overlapping sessions serialize snapshot rotation and leave no staging files', () async {
    final second = _FileStore(root);
    await Future.wait([
      for (var i = 0; i < 25; i++) (i.isEven ? store : second).write('session', {'turn': i}),
    ]);
    expect(await store.read('session'), {'turn': 24});
    expect(jsonDecode(await File(p.join(root.path, 'session.json.previous')).readAsString()), {'turn': 23});
    expect(
      (await root.list().toList()).map((e) => p.basename(e.path)),
      unorderedEquals(['session.json', 'session.json.previous']),
    );
  });

  test('a failed publication releases the queue and cleans its own staging directory', () async {
    final obstacle = await Directory(p.join(root.path, 'session.json')).create();
    await expectLater(store.write('session', {'turn': 1}), throwsA(isA<FileSystemException>()));
    expect(await root.list().map((e) => p.basename(e.path)).toList(), ['session.json']);
    await obstacle.delete();
    await store.write('session', {'turn': 2});
    expect(await store.read('session'), {'turn': 2});
  });
}
