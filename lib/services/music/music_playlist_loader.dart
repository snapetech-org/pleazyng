import 'dart:math' as math;

import '../../media/library_query.dart';
import '../../media/media_item.dart';
import '../../media/media_kind.dart';
import '../../media/media_server_client.dart';
import '../playlist_membership.dart';
import 'music_account_scope.dart';
import 'music_queue_items.dart';

/// A single authorized playlist read. Pages can be fetched around a selected
/// occurrence before the rest of the ordered playlist is materialized.
class MusicPlaylistRead {
  MusicPlaylistRead(this.client, this.id, {required this.current, this.acceptsItem, this.maxItems = 100000})
    : _authentication = client.authenticationSessionId,
      _account = musicAccountScope(client) {
    if (maxItems < 1 || maxItems > 100000) throw ArgumentError('Invalid playlist bound');
  }

  final MediaServerClient client;
  final String id;
  final bool Function() current;
  final bool Function(MediaItem)? acceptsItem;
  final int maxItems;
  final Object? _authentication;
  final String? _account;
  final _membership = PlaylistMembership();
  final _windows = <int, LibraryPage<MediaItem>>{};

  void _check(bool Function() active) {
    if (!active() ||
        !current() ||
        !identical(_authentication, client.authenticationSessionId) ||
        _account != musicAccountScope(client)) {
      throw StateError('Music playlist authorization changed');
    }
  }

  Future<LibraryPage<MediaItem>> window(int offset, {required bool Function() active}) async {
    final value = await _page(offset, active, 200);
    _windows[offset] = value;
    return value;
  }

  Future<LibraryPage<MediaItem>> _page(int offset, bool Function() active, int size) async {
    _check(active);
    final page = await client.fetchPlaylistPage(id, start: offset, size: size);
    _check(active);
    final total = _membership.check(page, start: offset, size: size);
    if (offset + page.items.length > maxItems || (!page.hasEstimatedTotal && total > maxItems)) {
      throw StateError('Music playlist exceeds native selection budget');
    }
    if (page.items.any(
      (item) =>
          item.id.isEmpty ||
          item.kind != MediaKind.track ||
          item.backend != client.backend ||
          item.serverId != client.serverId.value ||
          acceptsItem?.call(item) == false,
    )) {
      throw StateError('Native music playlist contains an unauthorized track');
    }
    return total == page.totalCount ? page : page.copyWith(totalCount: total, hasEstimatedTotal: false);
  }

  Future<List<MediaItem>> all({required bool Function() active}) async {
    final tracks = MusicQueueItems(spill: true);
    try {
      var offset = 0;
      int total;
      do {
        _check(active);
        LibraryPage<MediaItem>? existing;
        var size = 200;
        for (final page in _windows.values) {
          if (page.offset == offset) existing = page;
          if (page.offset > offset) size = math.min(size, page.offset - offset);
        }
        final next = existing ?? await _page(offset, active, size);
        _check(active);
        total = next.totalCount;
        tracks.addAll(next.items);
        offset += next.items.length;
      } while (offset < total);
      return tracks.snapshot();
    } finally {
      tracks.clear();
    }
  }
}
