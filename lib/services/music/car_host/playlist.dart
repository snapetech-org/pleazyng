part of '../music_car_host.dart';

extension _MusicCarPlaylist on _MusicCarHostState {
  Future<bool> _playOnlinePlaylist({
    required MediaItem item,
    required MediaItem selected,
    required int? start,
    required MusicReadScope scope,
    required Map<String, dynamic> args,
    required bool Function() sourceCurrent,
    required int intent,
    required Future<bool> Function() commit,
  }) async {
    bool pending() => sourceCurrent() && _music.isPlayIntentCurrent(intent);
    final read = MusicPlaylistRead(scope.client!, item.id, current: sourceCurrent, acceptsItem: scope.ownsTrack);
    final first = await read.window(0, active: pending);
    if (!pending() || first.items.isEmpty || !await commit() || !pending()) return false;
    final player = _music;
    final windowPlayer = player is MusicPlaylistWindowPlayback ? player as MusicPlaylistWindowPlayback : null;
    final queuing = args['queue'] == 'next' || args['queue'] == 'later';
    final shuffle = start == null && args['shuffle'] == true;
    final expandWindow =
        !queuing &&
        first.items.length < first.totalCount &&
        windowPlayer?.canPlayPlaylistWindow == true &&
        !(first.hasEstimatedTotal && shuffle);
    final index = start ?? (expandWindow && shuffle ? math.Random().nextInt(first.totalCount) : 0);
    if (index < 0 || index >= read.maxItems || (!first.hasEstimatedTotal && index >= first.totalCount)) return false;

    final window = index < first.items.length ? first : await read.window(index, active: pending);
    final localIndex = index - window.offset;
    if (!pending() || localIndex < 0 || localIndex >= window.items.length) return false;
    final target = window.items[localIndex];
    if (start != null &&
        (target.globalKey != selected.globalKey ||
            target.backend != selected.backend ||
            target.playlistItemId != selected.playlistItemId)) {
      return false;
    }

    final playContext = MusicPlayContext(title: item.displayTitle, kind: MusicPlayContextKind.playlist);
    if (expandWindow) {
      return windowPlayer!.playPlaylistWindow(
        tracks: window.items,
        offset: window.offset,
        index: localIndex,
        shuffle: shuffle,
        playContext: playContext,
        sourceCurrent: sourceCurrent,
        loadComplete: (active) => read.all(active: active),
      );
    }

    // Queue edits and remote output require an atomic ordered list. The native
    // owner has already accepted the request, so later pages can finish safely.
    final complete = first.items.length == first.totalCount ? first.items : await read.all(active: pending);
    if (!pending()) return false;
    if (_enqueueRequested(complete, args)) return true;
    if (start != null) {
      await player.playFromOccurrence(tracks: complete, index: index, playContext: playContext);
    } else {
      await player.playFromList(tracks: complete, playContext: playContext, shuffle: shuffle);
    }
    return player.isPlaying;
  }
}
