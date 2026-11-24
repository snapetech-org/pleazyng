import 'dart:io';
import 'dart:convert';
import 'package:crypto/crypto.dart';

import 'package:path/path.dart' as path;

import '../media/codec_catalog.dart';

part 'orphan_download_scan_checkpoint.dart';
part 'orphan_download_review_files.dart';

enum OrphanDownloadScanFailure { unsafeRoot, tooManyEntries, scanFailed, cancelled }

/// A file in Pleazyng's recognized, app-managed download layout that no stored
/// download row or native download task currently owns.
class OrphanDownloadFile {
  const OrphanDownloadFile({
    required this.relativePath,
    required this.itemTag,
    required this.sizeBytes,
    required this.modified,
  });

  final String relativePath;
  final String itemTag;
  final int sizeBytes;
  final DateTime modified;
}

class OrphanDownloadScan {
  const OrphanDownloadScan({
    required this.rootPath,
    required this.files,
    required this.failure,
    required this.scannedEntries,
  });

  final String rootPath;
  final List<OrphanDownloadFile> files;
  final OrphanDownloadScanFailure? failure;
  final int scannedEntries;

  bool get isComplete => failure == null;
  bool get canDelete => isComplete && files.isNotEmpty;
  int get totalBytes => files.fold(0, (sum, file) => sum + file.sizeBytes);
}

class OrphanDownloadCleanupResult {
  const OrphanDownloadCleanupResult({
    required this.deletedCount,
    required this.deletedBytes,
    required this.skippedCount,
  });

  final int deletedCount;
  final int deletedBytes;
  final int skippedCount;
}

/// Conservatively scans only the current tagged download layout. It never
/// follows symlinks, descends into unknown folders, touches artwork, or scans
/// legacy `{serverId}/{ratingKey}` directories.
class OrphanDownloadCleanupService {
  const OrphanDownloadCleanupService({this.maxEntries = 20000});

  static const _audioExtensions = {...CodecCatalog.audioExtensions, '.m4b'};
  static final _tagAtEnd = RegExp(r'\[([0-9a-f]{8})\](?: - part(?:[2-9]|[1-9][0-9]+))?$');
  static final _subtitleDirectoryTag = RegExp(r'\[([0-9a-f]{8})\](?: - part(?:[2-9]|[1-9][0-9]+))?_subs$');
  static final _subtitleFile = RegExp(r'^\d+\.(srt|ass|vtt|sup|sub)$', caseSensitive: false);
  static final _seasonDirectory = RegExp(r'^Season \d{2,}$');
  static final _itemTag = RegExp(r'^[0-9a-f]{8}$');

  final int maxEntries;

  Future<OrphanDownloadScan> scan(Directory root, {required Set<String> protectedTags}) async {
    final requestedRoot = path.normalize(path.absolute(root.path));
    try {
      final rootType = await FileSystemEntity.type(requestedRoot, followLinks: false);
      if (rootType == FileSystemEntityType.notFound) {
        return OrphanDownloadScan(rootPath: requestedRoot, files: const [], failure: null, scannedEntries: 0);
      }
      if (rootType != FileSystemEntityType.directory) {
        return OrphanDownloadScan(
          rootPath: requestedRoot,
          files: const [],
          failure: OrphanDownloadScanFailure.unsafeRoot,
          scannedEntries: 0,
        );
      }

      final canonicalRoot = path.normalize(await Directory(requestedRoot).resolveSymbolicLinks());
      final state = _ScanState(rootPath: canonicalRoot, protectedTags: protectedTags, maxEntries: maxEntries);
      for (final category in const ['Movies', 'TV Shows', 'Music']) {
        final categoryDir = Directory(path.join(canonicalRoot, category));
        if (!await _isRealDirectory(categoryDir)) continue;
        switch (category) {
          case 'Movies':
            for (final movieDir in await _directories(categoryDir, state)) {
              await _scanItemDirectory(movieDir, state, _Category.video, includeThumbnails: false);
            }
          case 'TV Shows':
            for (final showDir in await _directories(categoryDir, state)) {
              for (final seasonDir in await _directories(showDir, state, nameFilter: _seasonDirectory)) {
                await _scanItemDirectory(seasonDir, state, _Category.video, includeThumbnails: true);
              }
            }
          case 'Music':
            for (final artistDir in await _directories(categoryDir, state)) {
              for (final albumDir in await _directories(artistDir, state)) {
                await _scanItemDirectory(albumDir, state, _Category.audio, includeThumbnails: false);
              }
            }
        }
      }
      state.files.sort((a, b) => a.relativePath.compareTo(b.relativePath));
      return OrphanDownloadScan(
        rootPath: canonicalRoot,
        files: List.unmodifiable(state.files),
        failure: null,
        scannedEntries: state.scannedEntries,
      );
    } on _ScanLimitExceeded {
      return OrphanDownloadScan(
        rootPath: requestedRoot,
        files: const [],
        failure: OrphanDownloadScanFailure.tooManyEntries,
        scannedEntries: maxEntries,
      );
    } catch (_) {
      return OrphanDownloadScan(
        rootPath: requestedRoot,
        files: const [],
        failure: OrphanDownloadScanFailure.scanFailed,
        scannedEntries: 0,
      );
    }
  }

  Future<OrphanDownloadCleanupResult> delete(OrphanDownloadScan scan, {required Set<String> protectedTags}) async {
    if (!scan.canDelete) return const OrphanDownloadCleanupResult(deletedCount: 0, deletedBytes: 0, skippedCount: 0);

    final canonicalRoot = path.normalize(path.absolute(scan.rootPath));
    if (await FileSystemEntity.type(canonicalRoot, followLinks: false) != FileSystemEntityType.directory) {
      return OrphanDownloadCleanupResult(deletedCount: 0, deletedBytes: 0, skippedCount: scan.files.length);
    }
    final resolvedRoot = path.normalize(await Directory(canonicalRoot).resolveSymbolicLinks());
    if (!path.equals(resolvedRoot, canonicalRoot)) {
      return OrphanDownloadCleanupResult(deletedCount: 0, deletedBytes: 0, skippedCount: scan.files.length);
    }

    var deletedCount = 0;
    var deletedBytes = 0;
    var skippedCount = 0;
    for (final candidate in scan.files) {
      try {
        final file = await _matchingReviewedFile(canonicalRoot, candidate, protectedTags);
        if (file == null) {
          skippedCount++;
          continue;
        }
        await file.delete();
        deletedCount++;
        deletedBytes += candidate.sizeBytes;
      } on FileSystemException {
        skippedCount++;
      }
    }
    return OrphanDownloadCleanupResult(
      deletedCount: deletedCount,
      deletedBytes: deletedBytes,
      skippedCount: skippedCount,
    );
  }

  bool _isRecognizedCandidate(OrphanDownloadFile candidate, List<String> segments) {
    if (!_itemTag.hasMatch(candidate.itemTag)) return false;

    final category = segments.first;
    final fileName = segments.last;
    final extension = path.extension(fileName).toLowerCase();
    if (category == 'Movies' && segments.length == 3) {
      return CodecCatalog.videoExtensions.contains(extension) &&
          _extractTag(path.basenameWithoutExtension(fileName)) == candidate.itemTag;
    }
    if (category == 'TV Shows' && segments.length == 4 && _seasonDirectory.hasMatch(segments[2])) {
      return (CodecCatalog.videoExtensions.contains(extension) || extension == '.jpg') &&
          _extractTag(path.basenameWithoutExtension(fileName)) == candidate.itemTag;
    }
    if (category == 'Music' && segments.length == 4) {
      return _audioExtensions.contains(extension) &&
          _extractTag(path.basenameWithoutExtension(fileName)) == candidate.itemTag;
    }

    final subtitleDirectoryPathLength = switch (category) {
      'Movies' => 3,
      'TV Shows' => 4,
      _ => 0,
    };
    if (subtitleDirectoryPathLength == 0 || segments.length != subtitleDirectoryPathLength + 1) return false;
    if (category == 'TV Shows' && !_seasonDirectory.hasMatch(segments[2])) return false;

    final directoryTag = _subtitleDirectoryTag.firstMatch(segments[segments.length - 2])?.group(1);
    return directoryTag == candidate.itemTag && _subtitleFile.hasMatch(fileName);
  }

  Future<bool> _isRealDirectory(Directory directory) async =>
      await FileSystemEntity.type(directory.path, followLinks: false) == FileSystemEntityType.directory;

  Future<List<Directory>> _directories(Directory parent, _ScanState state, {RegExp? nameFilter}) async {
    final result = <Directory>[];
    await for (final entity in parent.list(followLinks: false)) {
      state.countEntry();
      if (await FileSystemEntity.type(entity.path, followLinks: false) != FileSystemEntityType.directory) continue;
      if (nameFilter != null && !nameFilter.hasMatch(path.basename(entity.path))) continue;
      if (!await state.isWithinRoot(entity.path)) continue;
      result.add(Directory(entity.path));
    }
    return result;
  }

  Future<void> _scanItemDirectory(
    Directory directory,
    _ScanState state,
    _Category category, {
    required bool includeThumbnails,
  }) async {
    await for (final entity in directory.list(followLinks: false)) {
      state.countEntry();
      final type = await FileSystemEntity.type(entity.path, followLinks: false);
      if (type == FileSystemEntityType.link) continue;
      if (type == FileSystemEntityType.directory) {
        final match = _subtitleDirectoryTag.firstMatch(path.basename(entity.path));
        if (match != null && !state.protectedTags.contains(match.group(1))) {
          await _scanSubtitleDirectory(Directory(entity.path), match.group(1)!, state);
        }
        continue;
      }
      if (type != FileSystemEntityType.file || !await state.isWithinRoot(entity.path)) continue;

      final extension = path.extension(entity.path).toLowerCase();
      final tag = _extractTag(path.basenameWithoutExtension(entity.path));
      if (tag == null || state.protectedTags.contains(tag)) continue;

      final recognized = switch (category) {
        _Category.video =>
          CodecCatalog.videoExtensions.contains(extension) || (includeThumbnails && extension == '.jpg'),
        _Category.audio => _audioExtensions.contains(extension),
      };
      if (recognized) await state.addCandidate(entity.path, tag);
    }
  }

  Future<void> _scanSubtitleDirectory(Directory directory, String tag, _ScanState state) async {
    if (!await state.isWithinRoot(directory.path)) return;
    await for (final entity in directory.list(followLinks: false)) {
      state.countEntry();
      if (await FileSystemEntity.type(entity.path, followLinks: false) != FileSystemEntityType.file ||
          !_subtitleFile.hasMatch(path.basename(entity.path)) ||
          !await state.isWithinRoot(entity.path)) {
        continue;
      }
      await state.addCandidate(entity.path, tag);
    }
  }

  static String? tagForStoredFile(String storedPath) => _extractTag(path.basenameWithoutExtension(storedPath));

  static String? _extractTag(String baseName) => _tagAtEnd.firstMatch(baseName.toLowerCase())?.group(1);
}

enum _Category { video, audio }

class _ScanState {
  _ScanState({required this.rootPath, required this.protectedTags, required this.maxEntries});

  final String rootPath;
  final Set<String> protectedTags;
  final int maxEntries;
  final List<OrphanDownloadFile> files = [];
  int scannedEntries = 0;

  void countEntry() {
    scannedEntries++;
    if (scannedEntries > maxEntries) throw const _ScanLimitExceeded();
  }

  Future<bool> isWithinRoot(String entityPath) async {
    try {
      final resolved = path.normalize(await File(entityPath).resolveSymbolicLinks());
      return path.isWithin(rootPath, resolved);
    } catch (_) {
      // best-effort: Refuse cleanup when the file cannot be resolved inside the selected root.
      return false;
    }
  }

  Future<void> addCandidate(String filePath, String tag) async {
    final file = File(filePath);
    final stat = await file.stat();
    files.add(
      OrphanDownloadFile(
        relativePath: path.relative(filePath, from: rootPath),
        itemTag: tag,
        sizeBytes: stat.size,
        modified: stat.modified,
      ),
    );
  }
}

class _ScanLimitExceeded implements Exception {
  const _ScanLimitExceeded();
}
