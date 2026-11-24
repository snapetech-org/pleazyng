part of '../download_manager_service.dart';

extension _ManagedDownloadRepair on DownloadManagerService {
  /// Backup restores may omit regenerable media. Keep affected rows available
  /// for manual retry and retain files this installation already has.
  Future<int> _repairManagedDownloads() async {
    if (!downloadsSupported) return 0;
    final root = path.normalize((await _storageService.getAppManagedDownloadsDirectory()).absolute.path);
    var repaired = 0;
    for (final row in await getAllDownloads()) {
      if (row.status != DownloadStatus.completed.index) continue;
      try {
        final parts = await _inspectManagedParts(row, root);
        if (parts == null || !parts.contains(null)) continue;
        final changed = await _database.transaction(() async {
          if (await _database.getDownloadedMedia(row.globalKey) != row) return false;
          await _database.storeDownloadParts(row.globalKey, parts.first, parts.skip(1).toList());
          await _database.updateBgTaskId(row.globalKey, null);
          await _database.updateDownloadProgress(row.globalKey, 0, 0, 0);
          await _database.updateDownloadStatus(row.globalKey, DownloadStatus.failed.index);
          await _database.updateDownloadError(row.globalKey, t.downloads.errorFileNotFound);
          await _database.removeFromQueue(row.globalKey);
          return true;
        });
        if (changed) {
          repaired++;
          _emitProgress(row.globalKey, DownloadStatus.failed, 0, errorMessage: t.downloads.errorFileNotFound);
        }
      } on FileSystemException catch (error) {
        appLogger.w('Could not check managed download after restore', error: error);
      }
    }
    return repaired;
  }

  Future<List<String?>?> _inspectManagedParts(DownloadedMediaItem row, String root) async {
    // With no recorded path, there is no evidence this row used managed storage.
    if (row.storedPartPaths.isEmpty) return null;
    final surviving = <String?>[];
    for (final stored in _partPathsOf(row)) {
      if (stored == null) {
        surviving.add(null);
        continue;
      }
      if (_storageService.isSafUri(stored)) return null;
      final absolute = path.normalize(await _storageService.ensureAbsolutePath(stored));
      if (!path.isWithin(root, absolute)) return null;
      surviving.add(await File(absolute).exists() ? stored : null);
    }
    return surviving;
  }
}
