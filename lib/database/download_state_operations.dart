part of 'download_operations.dart';

extension DownloadStateOperations on AppDatabase {
  /// Start a stacked download over. A zero count marks a single-file download.
  Future<void> resetDownloadParts(String globalKey, {required int additionalPartCount}) =>
      storeDownloadParts(globalKey, null, List<String?>.filled(additionalPartCount, null));

  /// Replace the saved paths together while retaining the previous completion time.
  Future<void> storeDownloadParts(String globalKey, String? first, List<String?> additional) async {
    await (update(downloadedMedia)..where((t) => t.globalKey.equals(globalKey))).write(
      DownloadedMediaCompanion(
        videoFilePath: Value(first),
        additionalPartPaths: Value(encodeAdditionalPartPaths(additional)),
      ),
    );
  }

  Future<void> updateDownloadStatus(String globalKey, int status) async {
    await (update(downloadedMedia)..where((t) => t.globalKey.equals(globalKey))).write(_downloadStatusChange(status));
  }

  /// Change status only while the row is still in one of [fromStatuses].
  /// Queue actions can overlap native terminal callbacks, so callers must not
  /// overwrite a completion or failure that committed while they were awaiting
  /// the downloader.
  Future<bool> updateDownloadStatusIfIn(String globalKey, int status, {required List<int> fromStatuses}) async {
    if (fromStatuses.isEmpty) return false;
    final changed =
        await (update(downloadedMedia)..where((row) => row.globalKey.equals(globalKey) & row.status.isIn(fromStatuses)))
            .write(_downloadStatusChange(status));
    return changed > 0;
  }
}

DownloadedMediaCompanion _downloadStatusChange(int status) => DownloadedMediaCompanion(
  status: Value(status),
  progress: status == DownloadStatus.completed.index ? const Value(100) : const Value.absent(),
);
