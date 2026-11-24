import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider_linux/path_provider_linux.dart';
import 'package:path_provider_windows/path_provider_windows.dart';
import 'package:shared_preferences_linux/shared_preferences_linux.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_async_platform_interface.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';
import 'package:shared_preferences_platform_interface/types.dart';
import 'package:shared_preferences_windows/shared_preferences_windows.dart';

class _LinuxDirectory extends Fake implements PathProviderLinux {
  _LinuxDirectory(this.root);
  final Directory root;
  @override
  Future<String?> getApplicationSupportPath() async => root.path;
}

class _WindowsDirectory extends Fake implements PathProviderWindows {
  _WindowsDirectory(this.root);
  final Directory root;
  @override
  Future<String?> getApplicationSupportPath() async => root.path;
}

Future<void> _writeLinuxBatch(String path, int writer) => Isolate.run(() async {
  final backend = SharedPreferencesAsyncLinux()..pathProvider = _LinuxDirectory(Directory(path));
  for (var i = 0; i < 12; i++) {
    await backend.setInt('writer-$writer-$i', i, const SharedPreferencesOptions());
  }
});

void main() {
  test('Linux isolates merge updates into an owner-only file', () async {
    final root = await Directory.systemTemp.createTemp('desktop-prefs-isolates-');
    addTearDown(() => root.delete(recursive: true));
    final file = File(p.join(root.path, 'shared_preferences.json'));
    final existing = 'música 🎵' * 16000;
    await file.writeAsString(jsonEncode({'existing': existing}));
    await Future.wait([for (var i = 0; i < 4; i++) _writeLinuxBatch(root.path, i)]);
    final prefs = jsonDecode(await file.readAsString()) as Map;
    expect(prefs, {
      'existing': existing,
      for (var w = 0; w < 4; w++)
        for (var i = 0; i < 12; i++) 'writer-$w-$i': i,
    });
    expect((await file.stat()).mode & 0x1ff, 0x180);
  }, skip: !Platform.isLinux);
  for (final windows in [false, true]) {
    group(windows ? 'Windows adapter' : 'Linux adapter', () {
      late Directory root;
      late SharedPreferencesAsyncPlatform backend;
      late SharedPreferencesStorePlatform legacy;
      SharedPreferencesOptions named(String name) =>
          windows ? SharedPreferencesWindowsOptions(fileName: name) : SharedPreferencesLinuxOptions(fileName: name);
      const defaults = SharedPreferencesOptions();
      const clearAll = ClearPreferencesParameters(filter: PreferencesFilters());
      setUp(() async {
        root = await Directory.systemTemp.createTemp('desktop-prefs-documents-');
        addTearDown(() => root.delete(recursive: true));
        if (windows) {
          final provider = _WindowsDirectory(root);
          backend = SharedPreferencesAsyncWindows()..pathProvider = provider;
          legacy = SharedPreferencesWindows()..pathProvider = provider;
        } else {
          final provider = _LinuxDirectory(root);
          backend = SharedPreferencesAsyncLinux()..pathProvider = provider;
          legacy = SharedPreferencesLinux()..pathProvider = provider;
        }
      });

      test('alternating named files keeps reads, writes and clears in their own document', () async {
        await backend.setString('onlyA', 'A', named('first'));
        expect(await backend.getString('onlyA', named('second')), isNull);
        await backend.setString('onlyB', 'B', named('second'));
        await backend.clear(clearAll, named('first'));
        expect(await backend.getString('onlyB', named('second')), 'B');
        expect(jsonDecode(await File(p.join(root.path, 'second.json')).readAsString()), {'onlyB': 'B'});
      });

      test('interleaved legacy and async writers retain unrelated keys on disk', () async {
        await backend.setString('first', '1', defaults);
        await legacy.setValue('String', 'flutter.old', '2');
        await Future.wait([backend.setString('second', '3', defaults), legacy.setValue('String', 'flutter.new', '4')]);
        expect(jsonDecode(await File(p.join(root.path, 'shared_preferences.json')).readAsString()), {
          'first': '1',
          'flutter.old': '2',
          'second': '3',
          'flutter.new': '4',
        });
        await legacy.remove('flutter.old');
        expect(await backend.getString('flutter.old', defaults), isNull);
      });

      test('failed writes and clears report failure without publishing in-memory changes', () async {
        await backend.setString('keep', 'value', defaults);
        final blocked = await Directory(p.join(root.path, 'shared_preferences.json.tmp')).create();
        await expectLater(backend.setString('lost', 'value', defaults), throwsA(isA<FileSystemException>()));
        await expectLater(backend.clear(clearAll, defaults), throwsA(isA<FileSystemException>()));
        expect(await legacy.setValue('String', 'flutter.failed', 'v'), isFalse);
        await blocked.delete();
        expect(await backend.getString('keep', defaults), 'value');
        expect(await backend.getString('lost', defaults), isNull);
        await backend.setString('retry', 'ok', defaults);
        expect(jsonDecode(await File(p.join(root.path, 'shared_preferences.json')).readAsString()), {
          'keep': 'value',
          'retry': 'ok',
        });
      });

      test('an empty first-launch document accepts the first setting', () async {
        final file = File(p.join(root.path, 'shared_preferences.json'));
        await file.create();
        expect(await backend.getString('theme', defaults), isNull);
        await backend.setString('theme', 'dark', defaults);
        expect(jsonDecode(await file.readAsString()), {'theme': 'dark'});
      });

      test('wrong-shaped documents are preserved for recovery', () async {
        final file = File(p.join(root.path, 'shared_preferences.json'));
        await file.writeAsString('[]');
        await expectLater(backend.setString('key', 'value', defaults), throwsFormatException);
        expect(await file.readAsString(), '[]');
      });
    });
  }
}
