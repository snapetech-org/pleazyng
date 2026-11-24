import '../media/library_query.dart';
import '../media/media_item.dart';
import '../media/media_server_client.dart';
import '../utils/media_server_http_client.dart';
import 'music/music_queue_items.dart';
import 'playlist_membership.dart';

const int playlistItemsPageSize = 200;

/// Page through every item in a playlist via the backend-neutral client API.
Future<List<MediaItem>> fetchAllPlaylistItems(
  MediaServerClient client,
  String playlistId, {
  int pageSize = playlistItemsPageSize,
  AbortController? abort,
  bool Function()? sourceCurrent,
  bool Function(MediaItem)? acceptsItem,
  bool spoolMetadata = false,
  bool requireComplete = false,
}) async {
  final buffer = spoolMetadata ? MusicQueueItems(spill: true) : null;
  final membership = requireComplete ? PlaylistMembership() : null;
  try {
    final items = await drainPages<MediaItem>(
      (start, size) async {
        if (sourceCurrent?.call() == false) throw StateError('Playlist source retired');
        final page = await client.fetchPlaylistPage(playlistId, start: start, size: size, abort: abort);
        if (sourceCurrent?.call() == false) throw StateError('Playlist source retired');
        membership?.check(page, start: start, size: size);
        if (acceptsItem != null && page.items.any((item) => !acceptsItem(item))) {
          throw StateError('Playlist page contains an unauthorized item');
        }
        return page;
      },
      pageSize: pageSize,
      abort: abort,
      accumulator: buffer,
    );
    return buffer?.snapshot() ?? items;
  } finally {
    buffer?.clear();
  }
}

/// Page through every item in a collection via the backend-neutral client API.
Future<List<MediaItem>> fetchAllCollectionItemsPaged(
  MediaServerClient client,
  String collectionId, {
  int pageSize = 100,
  String? libraryId,
  String? libraryTitle,
}) {
  if (pageSize <= 0) throw ArgumentError.value(pageSize, 'pageSize');
  final seen = <String>{};
  var minimumTotal = 0;
  return drainPages<MediaItem>((start, size) async {
    final page = await client.fetchCollectionPage(
      collectionId,
      start: start,
      size: size,
      libraryId: libraryId,
      libraryTitle: libraryTitle,
    );
    final end = start + page.items.length;
    if (page.offset != start ||
        end > page.totalCount ||
        page.totalCount < minimumTotal ||
        (page.items.isEmpty && start < page.totalCount) ||
        page.items.any((item) => !seen.add(item.globalKey))) {
      throw const FormatException('Incomplete collection membership');
    }
    // A backend without an exact total uses one lookahead row on a full
    // page. Only that sentinel may disappear on the next request.
    minimumTotal = page.totalCount == end + 1 && page.items.length == size ? end : page.totalCount;
    return page;
  }, pageSize: pageSize);
}
