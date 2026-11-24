part of '../../jellyfin_client.dart';

List<Map<String, dynamic>> _itemsArray(Object? data, {bool requireRows = false}) {
  final rows = data is Map<String, dynamic> ? data['Items'] : data;
  if (rows is List) {
    if (requireRows && rows.any((row) => row is! Map<String, dynamic>)) {
      throw const FormatException('Invalid playlist row');
    }
    return rows.whereType<Map<String, dynamic>>().toList();
  }
  if (requireRows) throw const FormatException('Missing playlist rows');
  return const [];
}

/// Builds a [LibraryPage] from an `/Items`-shaped response: the `Items` array
/// run through [map], plus the server's `TotalRecordCount` when it reports one.
/// Responses that omit it (or return a non-int) fall back to
/// [fallbackPageTotal], whose full-page sentinel keeps pagination enabled;
/// [singlePage] endpoints return everything at once, so a full page there means
/// the end of the list, not "there may be more".
LibraryPage<T> _pagedItems<T>(
  Object? data, {
  required int offset,
  required List<T> Function(List<Map<String, dynamic>>) map,
  int? requestedSize,
  bool singlePage = false,
  bool requireRows = false,
}) {
  final rawItems = _itemsArray(data, requireRows: requireRows);
  final rawTotal = data is Map<String, dynamic> ? data['TotalRecordCount'] : null;
  final fallbackTotal = singlePage
      ? offset + rawItems.length
      : fallbackPageTotal(offset: offset, itemCount: rawItems.length, requestedSize: requestedSize);
  final items = map(rawItems);
  if (requireRows && items.length != rawItems.length) {
    throw const FormatException('Unrecognized playlist entry');
  }
  return LibraryPage<T>(
    items: items,
    totalCount: rawTotal is int ? rawTotal : fallbackTotal,
    hasEstimatedTotal: rawTotal is! int && fallbackTotal > offset + rawItems.length,
    offset: offset,
  );
}
