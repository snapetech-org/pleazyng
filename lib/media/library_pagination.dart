part of 'library_query.dart';

/// Conservative total for a page whose backend omitted an exact count. A full
/// page adds one sentinel item so callers keep pagination enabled without
/// claiming to know the real total.
int fallbackPageTotal({required int offset, required int itemCount, int? requestedSize}) {
  final fullPage = requestedSize != null && requestedSize > 0 && itemCount >= requestedSize;
  return offset + itemCount + (fullPage ? 1 : 0);
}

/// Walk every page of a paginated endpoint and concatenate the results.
///
/// [fetchPage] receives a zero-based offset and [pageSize] and is called until
/// a page comes back empty, the accumulated count reaches the page's
/// [LibraryPage.totalCount], or — when [stopOnShortPage] is set — a page comes
/// back shorter than [pageSize]. The short-page break is for backends whose
/// total is unreliable; leave it off when the total is authoritative.
///
/// [onPage] receives the accumulated items after each intermediate page —
/// i.e. only when another request will follow — so callers can render while
/// pagination continues. It never fires for single-page listings or the final
/// page; the returned list covers those.
///
/// [abort] is checked before and after every request. Errors propagate.
/// An optional empty [accumulator] lets large listings spool metadata instead
/// of retaining every decoded item; its owner controls lifetime and snapshots.
Future<List<T>> drainPages<T>(
  Future<LibraryPage<T>> Function(int start, int size) fetchPage, {
  required int pageSize,
  AbortController? abort,
  bool stopOnShortPage = false,
  void Function(List<T> accumulated)? onPage,
  List<T>? accumulator,
}) async {
  if (pageSize <= 0) throw ArgumentError.value(pageSize, 'pageSize');
  final all = accumulator ?? <T>[];
  if (all.isNotEmpty) throw ArgumentError('A page accumulator must start empty');
  var start = 0;
  while (true) {
    abort?.throwIfAborted();
    final page = await fetchPage(start, pageSize);
    abort?.throwIfAborted();
    if (page.items.isEmpty) break;
    all.addAll(page.items);
    start += page.items.length;
    if (start >= page.totalCount) break;
    if (stopOnShortPage && page.items.length < pageSize) break;
    onPage?.call(all);
  }
  return all;
}
