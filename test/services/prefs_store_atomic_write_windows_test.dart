@TestOn('windows')
library;

import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider_windows/path_provider_windows.dart';
import 'package:plezy/services/prefs_recovery.dart';
import 'package:shared_preferences_platform_interface/types.dart';
import 'package:shared_preferences_windows/shared_preferences_windows.dart';

/// The vendored `shared_preferences_windows` atomic write, on real NTFS.
///
/// `prefs_store_atomic_write_test.dart` covers the same patch through the
/// Linux twin and runs everywhere, but it can only prove POSIX `rename(2)`.
/// The store this all exists to protect lives on Windows (#1732), and there
/// the replacement goes through `MoveFileExW` with MOVEFILE_REPLACE_EXISTING —
/// which a POSIX runner and a memory file system are both silent about. Run by
/// the windows-native-test job in CI.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late Directory support;
  late File store;
  late File staging;
  late SharedPreferencesAsyncWindows backend;

  const options = SharedPreferencesOptions();

  setUp(() async {
    support = await Directory.systemTemp.createTemp('plezy_atomic_write_win_');
    store = File(p.join(support.path, prefsStoreFileName));
    staging = File('${store.path}.tmp');
    backend = SharedPreferencesAsyncWindows()..pathProvider = _TempPathProviderWindows(support.path);
  });

  tearDown(() async {
    if (await support.exists()) await support.delete(recursive: true);
  });

  test('rename replaces an existing document on NTFS', () async {
    await backend.setString('theme', 'dark', options);
    await backend.setString('theme', 'light', options);

    expect(jsonDecode(await store.readAsString()), containsPair('theme', 'light'));
    expect(await staging.exists(), isFalse);
  });

  test('independent Windows writers preserve every key', () async {
    final path = support.path;
    await Future.wait([for (var i = 0; i < 4; i++) _writeBatch(path, i)]);
    expect(jsonDecode(await store.readAsString()), {
      for (var writer = 0; writer < 4; writer++)
        for (var key = 0; key < 12; key++) 'writer-$writer-$key': key,
    });
  });

  test('a replaced document is complete and parseable', () async {
    for (var i = 0; i < 25; i++) {
      await backend.setString('key$i', 'value$i', options);
      // Every intermediate state is a whole document, never a truncation.
      expect(PrefsRecovery.describeStoreDamage(await store.readAsBytes()), isNull);
    }

    final document = jsonDecode(await store.readAsString()) as Map<String, dynamic>;
    expect(document, containsPair('key0', 'value0'));
    expect(document, containsPair('key24', 'value24'));
  });

  test('a stale staging file is swept once the document reads cleanly', () async {
    await backend.setString('theme', 'dark', options);
    await staging.writeAsString('{"credential_vault_key_v1":"left-behind"', flush: true);

    final reader = SharedPreferencesAsyncWindows()..pathProvider = _TempPathProviderWindows(support.path);
    await reader.getPreferences(const GetPreferencesParameters(filter: PreferencesFilters()), options);

    expect(await staging.exists(), isFalse);
  });

  test('an open reader does not block the replacement', () async {
    // Windows keeps mandatory locks on open handles, and antivirus and Search
    // Indexer both hold the store open. A replacement that a reader can veto
    // would turn every preference write into a silent no-op on exactly the
    // machines most likely to have damaged the store in the first place.
    await backend.setString('theme', 'dark', options);
    final handle = await store.open();
    addTearDown(handle.close);

    await backend.setString('theme', 'light', options);

    expect(jsonDecode(await store.readAsString()), containsPair('theme', 'light'));
  });
}

Future<void> _writeBatch(String path, int writer) => Isolate.run(() async {
  final backend = SharedPreferencesAsyncWindows()..pathProvider = _TempPathProviderWindows(path);
  for (var key = 0; key < 12; key++) {
    await backend.setInt('writer-$writer-$key', key, const SharedPreferencesOptions());
  }
});

class _TempPathProviderWindows extends PathProviderWindows {
  _TempPathProviderWindows(this.supportPath);

  final String supportPath;

  @override
  Future<String?> getApplicationSupportPath() async => supportPath;
}
