import '../media/library_query.dart';
import '../media/media_item.dart';

/// Entry identity is independent of media identity: two copies of a song can
/// be selected and removed separately. Missing entry IDs never fall back.
String? playlistOccurrenceId(MediaItem item) => switch (item) {
  PlexMediaItem(:final playlistItemId?) when playlistItemId > 0 => 'p:$playlistItemId',
  JellyfinMediaItem(:final playlistItemId?) when playlistItemId.isNotEmpty => 'j:$playlistItemId',
  _ => null,
};

/// Validates what a playlist read has observed, including windows read out of
/// order. A backend's one-row lookahead asks for another page without promising
/// that another entry exists.
class PlaylistMembership {
  int? _exactTotal;
  int _observedEnd = 0;
  // Keep one entry ID per page. Repeated songs have distinct playlist entries.
  final _pageStarts = <String, int>{};

  int check(LibraryPage<MediaItem> page, {required int start, required int size}) {
    final length = page.items.length;
    final end = start + length;
    final count = page.totalCount;
    final exact = _exactTotal;
    if (start < 0 ||
        size <= 0 ||
        page.offset != start ||
        length > size ||
        end > count ||
        (length == 0 && end < count) ||
        (page.hasEstimatedTotal && (length != size || count != end + 1)) ||
        (exact != null && (end > exact || (!page.hasEstimatedTotal && count != exact))) ||
        (!page.hasEstimatedTotal && count < _observedEnd)) {
      throw const FormatException('Inconsistent playlist membership');
    }
    if (length > 0) {
      final entry = playlistOccurrenceId(page.items.first);
      if (entry != null && _pageStarts.putIfAbsent(entry, () => start) != start) {
        throw const FormatException('Repeated playlist page');
      }
    }
    if (end > _observedEnd) _observedEnd = end;
    if (!page.hasEstimatedTotal) _exactTotal = count;
    return _exactTotal ?? count;
  }
}
