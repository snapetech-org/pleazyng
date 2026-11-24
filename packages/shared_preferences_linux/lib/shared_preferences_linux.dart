// Copyright 2013 The Flutter Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

import 'dart:async';
import 'dart:convert' show json, utf8;
import 'dart:io' show Platform;

import 'package:file/file.dart';
import 'package:file/local.dart';
import 'package:flutter/foundation.dart' show debugPrint, visibleForTesting;
import 'package:path/path.dart' as path;
import 'package:path_provider_linux/path_provider_linux.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_async_platform_interface.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';
import 'package:shared_preferences_platform_interface/types.dart';

import 'src/linux_document_file.dart';

export 'src/linux_document_file.dart' show restrictLinuxPreferences, writePrivateLinuxPreferences;

const String _defaultFileName = 'shared_preferences';

const String _defaultPrefix = 'flutter.';

/// The Linux implementation of [SharedPreferencesStorePlatform].
///
/// This class implements the `package:shared_preferences` functionality for Linux.
class SharedPreferencesLinux extends SharedPreferencesStorePlatform {
  /// Deprecated instance of [SharedPreferencesLinux].
  /// Use [SharedPreferencesStorePlatform.instance] instead.
  @Deprecated('Use `SharedPreferencesStorePlatform.instance` instead.')
  static SharedPreferencesLinux instance = SharedPreferencesLinux();

  /// Registers the Linux implementation.
  static void registerWith() {
    SharedPreferencesStorePlatform.instance = SharedPreferencesLinux();
    // A temporary work-around for having two plugins contained in a single package.
    SharedPreferencesAsyncLinux.registerWith();
  }

  /// File system used to store to disk. Exposed for testing only.
  @visibleForTesting
  FileSystem fs = const LocalFileSystem();

  /// The path_provider_linux instance used to find the support directory.
  @visibleForTesting
  PathProviderLinux pathProvider = PathProviderLinux();

  Future<bool> _update(void Function(Map<String, Object>) change) async {
    try {
      await _updatePreferences(_defaultFileName, fs: fs, pathProvider: pathProvider, change: change);
      return true;
    } on FileSystemException catch (error) {
      debugPrint('Error saving preferences to disk: $error');
      return false;
    }
  }

  @override
  Future<bool> clear() async {
    return clearWithParameters(
      ClearParameters(
        filter: PreferencesFilter(prefix: _defaultPrefix),
      ),
    );
  }

  @override
  Future<bool> clearWithPrefix(String prefix) async {
    return clearWithParameters(ClearParameters(filter: PreferencesFilter(prefix: prefix)));
  }

  @override
  Future<bool> clearWithParameters(ClearParameters parameters) async {
    final PreferencesFilter filter = parameters.filter;

    return _update((preferences) => preferences.removeWhere(
        (key, _) => key.startsWith(filter.prefix) && (filter.allowList == null || filter.allowList!.contains(key))));
  }

  @override
  Future<Map<String, Object>> getAll() async {
    return getAllWithParameters(
      GetAllParameters(
        filter: PreferencesFilter(prefix: _defaultPrefix),
      ),
    );
  }

  @override
  Future<Map<String, Object>> getAllWithPrefix(String prefix) async {
    return getAllWithParameters(GetAllParameters(filter: PreferencesFilter(prefix: prefix)));
  }

  @override
  Future<Map<String, Object>> getAllWithParameters(GetAllParameters parameters) async {
    final PreferencesFilter filter = parameters.filter;
    final Map<String, Object> withPrefix = await _loadPreferences(_defaultFileName, fs: fs, pathProvider: pathProvider);
    withPrefix
        .removeWhere((String key, _) => !(key.startsWith(filter.prefix) && (filter.allowList?.contains(key) ?? true)));
    return withPrefix;
  }

  @override
  Future<bool> remove(String key) => _update((preferences) => preferences.remove(key));

  @override
  Future<bool> setValue(String valueType, String key, Object value) =>
      _update((preferences) => preferences[key] = value);
}

/// The Linux implementation of [SharedPreferencesAsyncPlatform].
///
/// This class implements the `package:shared_preferences` functionality for Linux.
base class SharedPreferencesAsyncLinux extends SharedPreferencesAsyncPlatform {
  /// Registers the Linux implementation.
  static void registerWith() {
    SharedPreferencesAsyncPlatform.instance = SharedPreferencesAsyncLinux();
  }

  /// File system used to store to disk. Exposed for testing only.
  @visibleForTesting
  FileSystem fs = const LocalFileSystem();

  /// The path_provider_linux instance used to find the support directory.
  @visibleForTesting
  PathProviderLinux pathProvider = PathProviderLinux();

  @override
  Future<Set<String>> getKeys(
    GetPreferencesParameters parameters,
    SharedPreferencesOptions options,
  ) async {
    return (await getPreferences(parameters, options)).keys.toSet();
  }

  @override
  Future<void> setString(
    String key,
    String value,
    SharedPreferencesOptions options,
  ) {
    return _setValue(key, value, options);
  }

  @override
  Future<void> setBool(
    String key,
    bool value,
    SharedPreferencesOptions options,
  ) {
    return _setValue(key, value, options);
  }

  @override
  Future<void> setDouble(
    String key,
    double value,
    SharedPreferencesOptions options,
  ) {
    return _setValue(key, value, options);
  }

  @override
  Future<void> setInt(
    String key,
    int value,
    SharedPreferencesOptions options,
  ) {
    return _setValue(key, value, options);
  }

  @override
  Future<void> setStringList(
    String key,
    List<String> value,
    SharedPreferencesOptions options,
  ) {
    return _setValue(key, value, options);
  }

  @override
  Future<String?> getString(
    String key,
    SharedPreferencesOptions options,
  ) async {
    return await _getValue(key, options) as String?;
  }

  @override
  Future<bool?> getBool(
    String key,
    SharedPreferencesOptions options,
  ) async {
    return await _getValue(key, options) as bool?;
  }

  @override
  Future<double?> getDouble(
    String key,
    SharedPreferencesOptions options,
  ) async {
    return await _getValue(key, options) as double?;
  }

  @override
  Future<int?> getInt(
    String key,
    SharedPreferencesOptions options,
  ) async {
    return await _getValue(key, options) as int?;
  }

  @override
  Future<List<String>?> getStringList(
    String key,
    SharedPreferencesOptions options,
  ) async {
    return (await _getValue(key, options) as List<Object?>?)?.cast<String>().toList();
  }

  @override
  Future<void> clear(ClearPreferencesParameters parameters, SharedPreferencesOptions options) async {
    final SharedPreferencesLinuxOptions linuxOptions =
        SharedPreferencesLinuxOptions.fromSharedPreferencesOptions(options);
    final PreferencesFilters filter = parameters.filter;
    await _updatePreferences(linuxOptions.fileName,
        fs: fs,
        pathProvider: pathProvider,
        change: (prefs) => prefs.removeWhere((key, _) => filter.allowList == null || filter.allowList!.contains(key)));
  }

  @override
  Future<Map<String, Object>> getPreferences(
    GetPreferencesParameters parameters,
    SharedPreferencesOptions options,
  ) async {
    return _readAll(parameters.filter.allowList, options);
  }

  /// Reloads preferences from file.
  @visibleForTesting
  Future<void> reload(
    SharedPreferencesLinuxOptions options,
  ) async {
    await _loadPreferences(options.fileName, fs: fs, pathProvider: pathProvider);
  }

  Future<Map<String, Object>> _readAll(
    Set<String>? allowList,
    SharedPreferencesOptions options,
  ) async {
    final SharedPreferencesLinuxOptions linuxOptions =
        SharedPreferencesLinuxOptions.fromSharedPreferencesOptions(options);
    final Map<String, Object> prefs = await _loadPreferences(linuxOptions.fileName, fs: fs, pathProvider: pathProvider);
    prefs.removeWhere((String key, _) => !(allowList?.contains(key) ?? true));
    return prefs;
  }

  Future<Object?> _getValue(String key, SharedPreferencesOptions options) async {
    final SharedPreferencesLinuxOptions linuxOptions =
        SharedPreferencesLinuxOptions.fromSharedPreferencesOptions(options);
    return (await _loadPreferences(linuxOptions.fileName, fs: fs, pathProvider: pathProvider))[key];
  }

  Future<void> _setValue(String key, Object value, SharedPreferencesOptions options) async {
    final SharedPreferencesLinuxOptions linuxOptions =
        SharedPreferencesLinuxOptions.fromSharedPreferencesOptions(options);
    await _updatePreferences(linuxOptions.fileName,
        fs: fs, pathProvider: pathProvider, change: (prefs) => prefs[key] = value);
  }
}

/// Gets the file where the preferences are stored.
Future<File?> _getLocalDataFile(
  String fileName, {
  FileSystem fs = const LocalFileSystem(),
  PathProviderLinux? pathProvider,
}) async {
  pathProvider = pathProvider ?? PathProviderLinux();
  final String? directory = await pathProvider.getApplicationSupportPath();
  if (directory == null) {
    return null;
  }
  final String fileLocation = path.join(directory, '$fileName.json');
  return fs.file(fileLocation);
}

/// Suffix of the staging file used by the atomic write below.
///
/// PLEZY DELTA. Deliberately a single fixed name rather than a stamped one:
/// this file holds the whole preference document, credentials included, so at
/// most one may ever exist and a later write must reuse it rather than
/// accumulate copies.
const String _stagingSuffix = '.tmp';

/// Run each synchronous file operation against the selected document. Its
/// stable lock file also coordinates independent desktop app processes.
T _withDocumentLock<T>(File file, {required bool writing, required T Function() run}) {
  if (file.fileSystem is! LocalFileSystem) return run();
  final lock = file.fileSystem.file('${file.path}.lock');
  if (Platform.isLinux) return withLinuxDocumentLock(lock.path, writing, run);
  final handle = writing ? lock.openSync(mode: FileMode.append) : lock.openSync();
  try {
    handle.lockSync(writing ? FileLock.blockingExclusive : FileLock.blockingShared);
    return run();
  } finally {
    handle.closeSync();
  }
}

Map<String, Object> _readDocument(File file) {
  if (!file.existsSync()) return <String, Object>{};
  final Object? data;
  try {
    final contents = file.readAsStringSync();
    if (contents.isEmpty) return <String, Object>{};
    data = json.decode(contents);
  } on FormatException {
    // A decoder exception can contain a snippet of the credential-bearing file.
    throw const FormatException('Unreadable preferences document');
  }
  if (data is! Map<String, dynamic> || data.containsValue(null)) {
    throw const FormatException('Invalid preferences document');
  }
  return Map<String, Object>.from(data);
}

Future<Map<String, Object>> _loadPreferences(
  String fileName, {
  required FileSystem fs,
  required PathProviderLinux pathProvider,
}) async {
  final file = await _getLocalDataFile(fileName, fs: fs, pathProvider: pathProvider);
  if (file == null || !file.existsSync()) return <String, Object>{};
  // Older stores may be on a read-only volume and have no lock file yet.
  final lock = fs.file('${file.path}.lock');
  final hasLock = lock.existsSync();
  if (!hasLock && !fs.file('${file.path}$_stagingSuffix').existsSync()) return _readDocument(file);
  try {
    return _withDocumentLock<Map<String, Object>>(file, writing: !hasLock, run: () {
      final data = _readDocument(file);
      _removeStagingFile(file);
      return data;
    });
  } on FileSystemException {
    // A legacy document remains readable when its directory cannot be
    // upgraded with a lock for stale-stage cleanup.
    if (hasLock || lock.existsSync()) rethrow;
    return _readDocument(file);
  }
}

Future<void> _updatePreferences(
  String fileName, {
  required FileSystem fs,
  required PathProviderLinux pathProvider,
  required void Function(Map<String, Object>) change,
}) async {
  final file = await _getLocalDataFile(fileName, fs: fs, pathProvider: pathProvider);
  if (file == null) throw const FileSystemException('No application support directory for preferences');
  if (!file.parent.existsSync()) file.parent.createSync(recursive: true);
  _withDocumentLock(file, writing: true, run: () {
    final data = _readDocument(file);
    change(data);
    _writeDocument(file, data);
  });
}

/// Deletes a stale staging file, best effort.
///
/// PLEZY DELTA. Never allowed to fail a read: the staging file is ours, and a
/// locked or already-removed one changes nothing about the document.
void _removeStagingFile(File localDataFile) {
  try {
    final File staged = localDataFile.fileSystem.file('${localDataFile.path}$_stagingSuffix');
    if (staged.existsSync()) {
      staged.deleteSync();
    }
  } catch (e) {
    debugPrint('Could not remove a stale preferences staging file: $e');
  }
}

void _writeDocument(File localDataFile, Map<String, Object> preferences) {
  final String stringMap = json.encode(preferences);

  // PLEZY DELTA: stage, flush, then rename over the target.
  //
  // Upstream calls `writeAsStringSync` straight onto the live document.
  // That opens with the default `FileMode.write`, which truncates first, so
  // every single preference write has a window in which the only copy of the
  // store on disk is empty or half-written. A crash, power loss, forced
  // reboot or antivirus interception inside that window leaves a document
  // that fails to parse on every subsequent launch, and the store holds the
  // credential-vault key, so the damage is not recoverable by rewriting it.
  //
  // Renaming into place is atomic for readers: `rename(2)` here,
  // `MoveFileExW` with MOVEFILE_REPLACE_EXISTING on Windows. The flush has
  // to come first, or the rename could publish a file whose contents were
  // never committed — the same corruption by a different route.
  //
  final File staged = localDataFile.fileSystem.file('${localDataFile.path}$_stagingSuffix');
  if (Platform.isLinux && staged.fileSystem is LocalFileSystem) {
    if (staged.fileSystem.typeSync(staged.path, followLinks: false) != FileSystemEntityType.notFound) {
      staged.deleteSync();
    }
    writePrivateLinuxPreferences(staged.path, utf8.encode(stringMap));
  } else {
    staged.writeAsStringSync(stringMap, flush: true);
  }
  staged.renameSync(localDataFile.path);
}

/// Linux specific SharedPreferences Options.
class SharedPreferencesLinuxOptions extends SharedPreferencesOptions {
  /// Constructor for SharedPreferencesLinuxOptions.
  const SharedPreferencesLinuxOptions({
    this.fileName = 'shared_preferences',
  });

  /// The name of the file to store preferences in.
  final String fileName;

  /// Returns a new instance of [SharedPreferencesLinuxOptions] from an existing
  /// [SharedPreferencesOptions].
  static SharedPreferencesLinuxOptions fromSharedPreferencesOptions(SharedPreferencesOptions options) {
    if (options is SharedPreferencesLinuxOptions) {
      return options;
    }
    return const SharedPreferencesLinuxOptions();
  }
}
