part of '../../plex_client.dart';

int? _reportedPlexTotal(MediaServerResponse response, Map<String, dynamic>? container) {
  for (final entry in response.headers.entries) {
    if (entry.key.toLowerCase() == 'x-plex-container-total-size') {
      final total = flexibleInt(entry.value);
      if (total != null) return total;
    }
  }
  return flexibleInt(container?['totalSize']);
}

({int total, bool estimated}) _plexPageCount({
  required int? reported,
  required Map<String, dynamic>? container,
  required int itemCount,
  int? start,
  int? size,
}) {
  if (reported != null) return (total: reported, estimated: false);
  if (start == null && size == null) {
    return (total: flexibleInt(container?['size']) ?? itemCount, estimated: false);
  }
  final offset = start ?? flexibleInt(container?['offset']) ?? 0;
  final total = fallbackPageTotal(offset: offset, itemCount: itemCount, requestedSize: size);
  return (total: total, estimated: total > offset + itemCount);
}

extension on _PlexClientInternals {
  Future<LibraryPage<MediaItem>> _playlistMembership(String id, {int? start, int? size, AbortController? abort}) async {
    final response = await _getWithFailover(
      '/playlists/$id/items',
      queryParameters: _buildPaginationParams(start, size),
      abort: abort,
    );
    final container = _getMediaContainer(response);
    final reported = _reportedPlexTotal(response, container);
    final rows = container?['Metadata'];
    final declaredSize = flexibleInt(container?['size']);
    final confirmedEmpty = rows == null && (declaredSize ?? reported) == 0;
    if (container == null || (rows is! List && !confirmedEmpty)) {
      throw const FormatException('Missing playlist rows');
    }
    final items = _extractMetadataList(response).map(PlexMappers.mediaItem).toList();
    if (items.any((item) => item.id.isEmpty)) throw const FormatException('Missing playlist item identity');
    final offset = flexibleInt(container['offset']) ?? start ?? 0;
    final count = _plexPageCount(
      reported: reported,
      container: container,
      itemCount: items.length,
      start: start == null ? null : offset,
      size: size,
    );
    return LibraryPage(items: items, offset: offset, totalCount: count.total, hasEstimatedTotal: count.estimated);
  }
}
