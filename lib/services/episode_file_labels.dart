part of 'episode_file_boundaries.dart';

final _rangeLabel = RegExp(r's(\d{1,3})e(\d{1,3})((?:(?:\s*-\s*e?|e)\d{1,3})+)(?![a-z0-9])', caseSensitive: false);
final _chapterLabel = RegExp(r'^\s*(?:Episode\s+|S(\d{1,3})E)(\d{1,3})(?:\s*$|\s*[-:–]\s*.+$)', caseSensitive: false);

({int season, int first, int last})? _episodeRange(String path) {
  final match = _rangeLabel.firstMatch(path.split(RegExp(r'[/\\]')).last);
  if (match == null) return null;
  final last = RegExp(r'\d+$').firstMatch(match[3]!)![0]!;
  return (season: int.parse(match[1]!), first: int.parse(match[2]!), last: int.parse(last));
}

int? _chapterEpisode(String? title, int? season) {
  final match = _chapterLabel.firstMatch(title ?? '');
  if (match == null) return null;
  final chapterSeason = int.tryParse(match[1] ?? '');
  if (chapterSeason != null && season != null && chapterSeason != season) return null;
  return int.parse(match[2]!);
}

extension _EpisodeStarts on EpisodeFileBounds {
  Map<int, int> _mergeStarts(List<MediaChapter> chapters, Map<String, dynamic> saved) {
    final values = <int, int>{first: 0};
    final duplicates = <int>{};
    for (final c in chapters) {
      final n = _chapterEpisode(c.title, season), pos = c.startTimeOffset;
      if (n != null && pos != null && n > first && n <= last && pos > 0 && pos < duration) {
        if (values.containsKey(n)) duplicates.add(n);
        values[n] = pos;
      }
    }
    for (final n in duplicates) {
      values.remove(n);
    }
    for (final e in saved.entries) {
      final n = int.tryParse(e.key), v = e.value;
      if (n != null && n > first && n <= last && v is int && v > 0 && v < duration) values[n] = v;
    }
    return values;
  }
}
