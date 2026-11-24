import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as path;
import 'package:plezy/services/download_storage_service.dart';
import 'package:plezy/services/orphan_download_cleanup_service.dart';

void main() {
  late Directory scratch;
  late Directory downloads;

  setUp(() async {
    scratch = await Directory.systemTemp.createTemp('plezyng-orphan-cleanup-test-');
    downloads = Directory(path.join(scratch.path, 'downloads'));
    await downloads.create();
  });

  tearDown(() async {
    if (await scratch.exists()) await scratch.delete(recursive: true);
  });

  Future<File> write(String relativePath, List<int> bytes) async {
    final file = File(path.join(downloads.path, relativePath));
    await file.parent.create(recursive: true);
    return file..writeAsBytesSync(bytes);
  }

  test('scans only tagged media and subtitles in the current download layout', () async {
    final orphanKey = 'plex-server:orphan-movie';
    final mpegTsKey = 'plex-server:orphan-mpegts';
    final webmaKey = 'plex-server:orphan-webma';
    final episodeKey = 'plex-server:orphan-episode';
    final activeKey = 'plex-server:active-track';
    final orphanTag = DownloadStorageService.tagForGlobalKey(orphanKey);
    final mpegTsTag = DownloadStorageService.tagForGlobalKey(mpegTsKey);
    final webmaTag = DownloadStorageService.tagForGlobalKey(webmaKey);
    final episodeTag = DownloadStorageService.tagForGlobalKey(episodeKey);
    final activeTag = DownloadStorageService.tagForGlobalKey(activeKey);

    await write('Movies/Example Movie (2020)/Example Movie [$orphanTag].mkv', [1, 2, 3]);
    await write('Movies/Transport/Transport [$mpegTsTag].mpegts', [1]);
    await write('Movies/Transport/Transport [$mpegTsTag] - part2.IVF', [1]);
    await write('Music/Example Artist/Example Album/03 - Track [$webmaTag].DTSHD', [1]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag].mp4', [1, 2]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag].jpg', [1]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag]_subs/12.srt', [1, 2, 3, 4]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag] - part2_subs/12.srt', [1]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag] - part10_subs/12.srt', [1]);
    await write('TV Shows/Example Show/Season 01/S01E01 - Pilot [$episodeTag]_subs/13.idx', [1]);
    await write('Music/Example Artist/Example Album/01 - Track [$activeTag].flac', [1, 2, 3, 4, 5]);
    await write('Music/Example Artist/Example Album/02 - Track [$webmaTag].webma', [1]);

    await write('Movies/Example Movie (2020)/untagged.mp4', [1]);
    await write('Movies/Example Movie (2020)/Example Movie [1234abcd].exe', [1]);
    await write('TV Shows/Example Show/Season 01/S01E02 - Unknown [1234abcd]_subs/metadata.json', [1]);
    await write('legacy-server/123/video.mp4', [1]);
    await write('artwork/1234567890.jpg', [1]);

    final scan = await const OrphanDownloadCleanupService().scan(downloads, protectedTags: {activeTag});

    expect(scan.isComplete, isTrue);
    expect(scan.files.map((file) => file.relativePath).toSet(), {
      path.join('Movies', 'Example Movie (2020)', 'Example Movie [$orphanTag].mkv'),
      path.join('Movies', 'Transport', 'Transport [$mpegTsTag].mpegts'),
      path.join('Movies', 'Transport', 'Transport [$mpegTsTag] - part2.IVF'),
      path.join('Music', 'Example Artist', 'Example Album', '03 - Track [$webmaTag].DTSHD'),
      path.join('TV Shows', 'Example Show', 'Season 01', 'S01E01 - Pilot [$episodeTag].mp4'),
      path.join('TV Shows', 'Example Show', 'Season 01', 'S01E01 - Pilot [$episodeTag].jpg'),
      path.join('TV Shows', 'Example Show', 'Season 01', 'S01E01 - Pilot [$episodeTag]_subs', '12.srt'),
      path.join('TV Shows', 'Example Show', 'Season 01', 'S01E01 - Pilot [$episodeTag] - part2_subs', '12.srt'),
      path.join('TV Shows', 'Example Show', 'Season 01', 'S01E01 - Pilot [$episodeTag] - part10_subs', '12.srt'),
      path.join('Music', 'Example Artist', 'Example Album', '02 - Track [$webmaTag].webma'),
    });
    expect(scan.totalBytes, 16);
  });

  test('rechecks retained tags before deletion', () async {
    final firstTag = DownloadStorageService.tagForGlobalKey('server:first');
    final secondTag = DownloadStorageService.tagForGlobalKey('server:second');
    final first = await write('Movies/A/A [$firstTag].mp4', [1, 2, 3]);
    final second = await write('Movies/B/B [$secondTag].mp4', [4, 5]);
    final service = const OrphanDownloadCleanupService();
    final scan = await service.scan(downloads, protectedTags: const {});

    final result = await service.delete(scan, protectedTags: {firstTag});

    expect(result.deletedCount, 1);
    expect(result.deletedBytes, 2);
    expect(result.skippedCount, 1);
    expect(await first.exists(), isTrue);
    expect(await second.exists(), isFalse);
  });

  test('keeps files replaced after the review scan', () async {
    final changedSizeTag = DownloadStorageService.tagForGlobalKey('server:changed-size');
    final changedTimeTag = DownloadStorageService.tagForGlobalKey('server:changed-time');
    final changedSize = await write('Movies/A/A [$changedSizeTag].mp4', [1, 2]);
    final changedTime = await write('Movies/B/B [$changedTimeTag].mp4', [3, 4]);
    final service = const OrphanDownloadCleanupService();
    final scan = await service.scan(downloads, protectedTags: const {});

    await changedSize.writeAsBytes([1, 2, 3]);
    await changedTime.writeAsBytes([5, 6]);
    await changedTime.setLastModified((await changedTime.lastModified()).add(const Duration(seconds: 10)));

    final result = await service.delete(scan, protectedTags: const {});

    expect(result.deletedCount, 0);
    expect(result.skippedCount, 2);
    expect(await changedSize.readAsBytes(), [1, 2, 3]);
    expect(await changedTime.readAsBytes(), [5, 6]);
  });

  test('rejects paths that were not produced by the recognized download layout', () async {
    final tag = DownloadStorageService.tagForGlobalKey('server:forged');
    final keep = await write('Movies/Personal/notes.txt', [1, 2, 3]);
    final fabricated = OrphanDownloadScan(
      rootPath: downloads.path,
      files: [
        OrphanDownloadFile(
          relativePath: path.join('Movies', 'Personal', 'notes.txt'),
          itemTag: tag,
          sizeBytes: 3,
          modified: await keep.lastModified(),
        ),
      ],
      failure: null,
      scannedEntries: 1,
    );

    final result = await const OrphanDownloadCleanupService().delete(fabricated, protectedTags: const {});

    expect(result.deletedCount, 0);
    expect(result.skippedCount, 1);
    expect(await keep.exists(), isTrue);
  });

  test('does not delete a tagged file reached through a symlinked parent', () async {
    final tag = DownloadStorageService.tagForGlobalKey('server:parent-link');
    final outside = Directory(path.join(scratch.path, 'outside'))..createSync();
    final keep = File(path.join(outside.path, 'Escape [$tag].mp4'))..writeAsStringSync('keep');
    final link = Link(path.join(downloads.path, 'Movies', 'Escape'));
    await link.parent.create(recursive: true);
    try {
      await link.create(outside.path);
    } on FileSystemException {
      return;
    } on UnsupportedError {
      return;
    }

    final fabricated = OrphanDownloadScan(
      rootPath: downloads.path,
      files: [
        OrphanDownloadFile(
          relativePath: path.join('Movies', 'Escape', 'Escape [$tag].mp4'),
          itemTag: tag,
          sizeBytes: 4,
          modified: await keep.lastModified(),
        ),
      ],
      failure: null,
      scannedEntries: 1,
    );
    final result = await const OrphanDownloadCleanupService().delete(fabricated, protectedTags: const {});

    expect(result.deletedCount, 0);
    expect(result.skippedCount, 1);
    expect(await keep.exists(), isTrue);
  });

  test('does not follow symlinks outside the managed root', () async {
    final tag = DownloadStorageService.tagForGlobalKey('server:linked');
    final target = File(path.join(scratch.path, 'outside [$tag].mp4'))..writeAsStringSync('keep');
    final link = Link(path.join(downloads.path, 'Movies', 'Linked', 'Linked [$tag].mp4'));
    await link.parent.create(recursive: true);
    try {
      await link.create(target.path);
    } on FileSystemException {
      return;
    } on UnsupportedError {
      return;
    }

    final scan = await const OrphanDownloadCleanupService().scan(downloads, protectedTags: const {});
    expect(scan.files, isEmpty);
    expect(await target.exists(), isTrue);
  });

  test('a resumed scan includes files whose downloads stopped protecting them', () async {
    const tag = '1234abcd';
    await write('Movies/A/A [$tag].mp4', [1, 2, 3]);
    await write('Movies/Z/Z [5678abcd].mp4', [4]);
    final cache = Directory(path.join(scratch.path, 'cache'));
    const service = OrphanDownloadCleanupService(maxEntries: 3);
    var cancelled = false;
    final partial = await service.scanWithCheckpoint(
      downloads,
      cache,
      protectedTags: {tag},
      onProgress: (count) {
        if (count >= 3) cancelled = true;
      },
      cancelled: () => cancelled,
    );
    expect(partial.failure, OrphanDownloadScanFailure.cancelled);

    final resumed = await service.scanWithCheckpoint(downloads, cache, protectedTags: {});
    expect(resumed.files.map((file) => file.itemTag), [tag, '5678abcd']);
  });

  test('a resumed scan excludes files removed or replaced while it was stopped', () async {
    final removed = await write('Movies/A/A [1234abcd].mp4', [1]);
    final replaced = await write('Movies/B/B [5678abcd].mp4', [2]);
    final kept = await write('Movies/C/C [12345678].mp4', [3]);
    await write('Movies/Z/Z [abcd1234].mp4', [4]);
    final cache = Directory(path.join(scratch.path, 'cache'));
    const service = OrphanDownloadCleanupService(maxEntries: 7);
    var cancelled = false;
    final partial = await service.scanWithCheckpoint(
      downloads,
      cache,
      protectedTags: {},
      onProgress: (count) {
        if (count >= 7) cancelled = true;
      },
      cancelled: () => cancelled,
    );
    expect(partial.failure, OrphanDownloadScanFailure.cancelled);
    await removed.delete();
    await replaced.writeAsBytes([2, 2]);

    final progress = <int>[];
    final resumed = await service.scanWithCheckpoint(downloads, cache, protectedTags: {}, onProgress: progress.add);
    expect(progress.first, partial.scannedEntries);
    expect(resumed.files.map((file) => file.itemTag), ['12345678', 'abcd1234']);
    expect(await kept.exists(), isTrue);
    expect(await replaced.readAsBytes(), [2, 2]);
  });

  test('cancelling while finalizing the scan leaves it resumable and unavailable for deletion', () async {
    await write('Movies/A/A [1234abcd].mp4', [1, 2, 3]);
    final cache = Directory(path.join(scratch.path, 'cache'));
    const service = OrphanDownloadCleanupService();
    var cancelled = false;
    final partial = await service.scanWithCheckpoint(
      downloads,
      cache,
      protectedTags: {},
      onProgress: (_) {
        cancelled = true;
      },
      cancelled: () => cancelled,
    );
    expect(partial.failure, OrphanDownloadScanFailure.cancelled);
    expect(partial.canDelete, isFalse);

    final resumed = await service.scanWithCheckpoint(downloads, cache, protectedTags: {});
    expect(resumed.files.single.itemTag, '1234abcd');
    expect(resumed.canDelete, isTrue);
  });

  test('refuses deletion after a bounded scan is incomplete', () async {
    final tag = DownloadStorageService.tagForGlobalKey('server:bounded');
    final file = await write('Movies/A/A [$tag].mp4', [1]);
    final service = const OrphanDownloadCleanupService(maxEntries: 1);
    final scan = await service.scan(downloads, protectedTags: const {});

    expect(scan.failure, OrphanDownloadScanFailure.tooManyEntries);
    expect(scan.canDelete, isFalse);
    final result = await service.delete(scan, protectedTags: const {});
    expect(result.deletedCount, 0);
    expect(await file.exists(), isTrue);
  });
}
