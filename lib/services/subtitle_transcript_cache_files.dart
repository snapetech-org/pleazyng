part of 'subtitle_source_transcript_cache.dart';

const _stagingPrefix = '.transcript-write-';

Future<void> _publishTranscriptFile(File file, Uint8List bytes) async {
  final directory = await file.parent.createTemp(_stagingPrefix);
  try {
    final staged = File(p.join(directory.path, 'data'));
    await staged.writeAsBytes(bytes, flush: true);
    await staged.rename(file.path);
  } finally {
    try {
      await directory.delete(recursive: true);
    } on FileSystemException {
      // A later cache maintenance pass cleans abandoned writes.
    }
  }

  final root = file.parent.parent;
  final staleBefore = DateTime.now().subtract(const Duration(days: 1));
  final all = <_TranscriptFile>[];
  await for (final profile in root.list(followLinks: false)) {
    if (profile is! Directory) continue;
    final entries = <_TranscriptFile>[];
    try {
      await for (final entry in profile.list(followLinks: false)) {
        if (entry is File && entry.path.endsWith('.json')) {
          final stat = await entry.stat();
          if (stat.type == FileSystemEntityType.file) entries.add((file: entry, stat: stat));
        } else if ((entry is Directory && p.basename(entry.path).startsWith(_stagingPrefix)) ||
            (entry is File && entry.path.endsWith('.pending'))) {
          try {
            if ((await entry.stat()).modified.isBefore(staleBefore)) await entry.delete(recursive: entry is Directory);
          } on FileSystemException {
            // A busy staging file need not prevent eviction of older transcripts.
          }
        }
      }
      if (profile.path == file.parent.path) {
        await _trimTranscriptFiles(
          entries,
          SubtitleSourceTranscriptCacheService.maximumEntriesPerProfile,
          SubtitleSourceTranscriptCacheService.maximumBytesPerProfile,
        );
      } else if (entries.isEmpty) {
        await profile.delete(); // Only succeeds when there is no active staging directory.
      }
    } on FileSystemException {
      // Another process may be removing or populating this profile.
    }
    all.addAll(entries);
  }
  await _trimTranscriptFiles(
    all,
    SubtitleSourceTranscriptCacheService.maximumTotalEntries,
    SubtitleSourceTranscriptCacheService.maximumTotalBytes,
  );
}

typedef _TranscriptFile = ({File file, FileStat stat});

Future<void> _trimTranscriptFiles(List<_TranscriptFile> entries, int maxCount, int maxBytes) async {
  var count = entries.length, bytes = entries.fold<int>(0, (total, e) => total + e.stat.size);
  if (count <= maxCount && bytes <= maxBytes) return;
  entries.sort((a, b) => a.stat.modified.compareTo(b.stat.modified));
  final removed = <_TranscriptFile>{};
  for (final entry in entries) {
    if (count <= maxCount && bytes <= maxBytes) break;
    try {
      await entry.file.delete();
    } on PathNotFoundException {
      // The quota has already been released by another writer.
    } on FileSystemException {
      continue;
    }
    removed.add(entry);
    count--;
    bytes -= entry.stat.size;
  }
  entries.removeWhere(removed.contains);
}
