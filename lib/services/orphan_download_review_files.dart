part of 'orphan_download_cleanup_service.dart';

extension _OrphanDownloadReviewFiles on OrphanDownloadCleanupService {
  Future<File?> _matchingReviewedFile(String root, OrphanDownloadFile candidate, Set<String> protectedTags) async {
    if (protectedTags.contains(candidate.itemTag)) return null;
    final segments = path.split(candidate.relativePath);
    if (path.isAbsolute(candidate.relativePath) ||
        segments.isEmpty ||
        segments.any((segment) => segment.isEmpty || segment == '.' || segment == '..') ||
        !_isRecognizedCandidate(candidate, segments)) {
      return null;
    }

    final filePath = path.normalize(path.joinAll([root, ...segments]));
    if (!path.isWithin(root, filePath) ||
        !await _hasRealDirectoryParents(root, segments) ||
        await FileSystemEntity.type(filePath, followLinks: false) != FileSystemEntityType.file) {
      return null;
    }

    final file = File(filePath);
    final resolved = path.normalize(await file.resolveSymbolicLinks());
    if (!path.equals(resolved, filePath)) return null;
    final stat = await file.stat();
    return stat.type == FileSystemEntityType.file &&
            stat.size == candidate.sizeBytes &&
            stat.modified == candidate.modified
        ? file
        : null;
  }

  Future<List<OrphanDownloadFile>?> _restoreReviewedFiles(
    Directory cache,
    String root,
    int chunks,
    Set<String> protectedTags,
    bool Function()? cancelled,
  ) async {
    final files = <OrphanDownloadFile>[];
    try {
      for (var n = 0; n < chunks; n++) {
        final rows = jsonDecode(await File(path.join(cache.path, '$n.json')).readAsString()) as List;
        for (final row in rows) {
          if (cancelled?.call() == true) return null;
          final candidate = OrphanDownloadFile(
            relativePath: row[0],
            itemTag: row[1],
            sizeBytes: row[2],
            modified: DateTime.fromMicrosecondsSinceEpoch(row[3]),
          );
          try {
            if (await _matchingReviewedFile(root, candidate, protectedTags) != null) files.add(candidate);
          } on FileSystemException {
            // A file can disappear between the type check and stat.
          }
        }
      }
    } on Object {
      // A damaged cache must not strand every later review on the same cursor.
      await cache.delete(recursive: true);
      rethrow;
    }
    if (cancelled?.call() == true) return null;
    return files..sort((a, b) => a.relativePath.compareTo(b.relativePath));
  }

  Future<bool> _hasRealDirectoryParents(String rootPath, List<String> segments) async {
    var currentPath = rootPath;
    for (final segment in segments.take(segments.length - 1)) {
      currentPath = path.join(currentPath, segment);
      if (await FileSystemEntity.type(currentPath, followLinks: false) != FileSystemEntityType.directory) return false;
    }
    return true;
  }
}
