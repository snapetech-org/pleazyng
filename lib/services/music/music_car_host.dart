import '../../screens/music/jukebox_dialog.dart';
import '../../services/car_ux_restrictions_service.dart';
import '../../utils/platform_detector.dart';
import 'server_music_radio.dart';
import '../playback_coordinator.dart';
import '../../utils/serial_future_queue.dart';
import 'guest_jukebox.dart';
import '../../karaoke/karaoke_room_server.dart';
import 'music_experience.dart';
import 'music_driving_routes.dart';
import 'music_system_audio_route_monitor.dart';

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart' show defaultTargetPlatform;
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:uuid/uuid.dart';

import '../../database/app_database.dart';
import '../../i18n/strings.g.dart';
import '../../media/ids.dart';
import '../../media/library_query.dart';
import '../../media/media_filter.dart';
import '../../media/media_item.dart';
import '../../media/media_part_timeline.dart';
import '../../media/media_kind.dart';
import '../../media/media_server_client.dart';
import '../../profiles/active_profile_provider.dart';
import '../../providers/download_provider.dart';
import '../../providers/hidden_libraries_provider.dart';
import '../../providers/libraries_provider.dart';
import '../../providers/multi_server_provider.dart';
import '../../screens/music/music_driving_screen.dart';
import '../../services/settings_service.dart';
import '../../sources/publication_shelf.dart';
import '../../sources/offline_readiness.dart';
import '../../widgets/music/music_favorites_shelf.dart';
import '../../sources/source_controller.dart';
import '../../sources/source_models.dart';
import '../../sources/source_playback.dart';
import '../../utils/provider_extensions.dart';
import '../../utils/formatters.dart' show formatDurationTimestamp;
import '../../utils/music_read_scope.dart';
import 'music_tracks_loader.dart';
import '../../utils/media_server_http_client.dart';
import 'music_car_artwork.dart';
import 'music_queue_controller.dart';
import 'music_discovery.dart';
import 'music_playback_service.dart';
import 'music_library_store.dart';
import 'music_station.dart';
import 'music_account_scope.dart';
import 'music_background_frames.dart';
import 'music_offline_bundle.dart';
import 'music_playlist_loader.dart';

part 'music_car_video.dart';

part 'car_host/browse.dart';
part 'car_host/playlist.dart';

/// Profile-scoped native browse/command boundary. Native displays carry opaque
/// references, text and bounded artwork bytes; never media URLs, server tokens or filesystem paths.
class MusicCarHost extends StatefulWidget {
  const MusicCarHost({super.key, required this.navigatorKey, required this.child});
  final GlobalKey<NavigatorState> navigatorKey;
  final Widget child;
  @override
  State<MusicCarHost> createState() => _MusicCarHostState();
}

class _MusicCarHostState extends State<MusicCarHost> with WidgetsBindingObserver {
  static const _channel = MethodChannel('com.pleazyng/music_car');
  static Object? _owner;
  final Object _identity = Object();
  final String _itemScope = const Uuid().v4();
  int _itemSequence = 0;
  int _playlistGeneration = 0;
  late final MusicPlaybackService _music;
  late final String? _profileId;
  late final ActiveProfileProvider _profiles;
  final _carArtwork = MusicCarArtwork();
  final _artworkHttp = MediaServerHttpClient();
  final _artworkItems =
      <String, ({MediaItem item, MediaServerClient? client, Object? authentication, String? scope})>{};
  final Map<String, MediaItem> _items = {};
  final _playlistRevisions = <String, String>{};
  final _ownedByServer = <(String, String?), Map<String, MediaItem>>{};
  int _downloadGeneration = 0;
  final _parents = <String, ({MediaItem item, int index})>{};
  final Map<String, SourceEntry> _audiobookEntries = {};
  final _offlineSpoken = <String>{};
  SourceController? _sourceController;
  int _audiobookSequence = 0;
  bool _audiobookRefreshPending = false;
  final _serverRadios = <String, ({ServerMusicRadio radio, MediaServerClient client, bool Function() current})>{};
  final _authorizations =
      <String, ({MediaServerClient? client, Object? authentication, String? account, int generation})>{};
  Object? _queueShape;
  Object? _publishedStatus;
  DateTime _lastStatus = DateTime.fromMillisecondsSinceEpoch(0);
  SourcePlayback? _spokenPlayer;
  final _offlineRows = <String>{};
  bool _isEpisode(SourceEntry entry) =>
      entry.kind != SourceItemKind.video &&
      (entry.identifiers['podcast'] == 'episode' || entry.identifiers.containsKey('feed'));
  bool _isSpoken(SourceEntry? e) => e != null && (e.kind == SourceItemKind.audiobook || _isEpisode(e));
  SourcePlayback? get _spoken =>
      _isSpoken(_spokenPlayer?.entry) && _spokenPlayer?.video == false ? _spokenPlayer : null;

  String? _catalogShape;
  final List<Listenable> _catalogSources = [];
  bool _catalogPending = false;
  MusicExperience? _experience;
  void _pinsChanged() {
    if (_current) unawaited(_send('catalogChanged'));
  }

  MusicDrivingRoutes? _drivingRoutes;
  MusicSystemAudioRouteMonitor? _routeMonitor;
  bool _bluetoothConnected = false;
  bool _videoCapable = false;
  final _videoCommands = SerialFutureQueue();
  _CarVideoSession? _video;
  int _videoIntent = 0;
  late final Future<void> Function() _retireVideo = _stopCarVideo;
  bool _connected = false, _drivingOpen = false;
  Route<void>? _automaticDrivingRoute;
  int _catalogRevision = 0;

  bool get _current =>
      mounted &&
      identical(_owner, _identity) &&
      _profiles.activeId == _profileId &&
      !_profiles.isBinding &&
      _profiles.lastBindingSucceeded;

  @override
  void initState() {
    super.initState();
    _owner = _identity;
    _profiles = context.read<ActiveProfileProvider>();
    _profileId = _profiles.activeId;
    _music = context.read<MusicPlaybackService>()..addListener(_publish);
    _spokenPlayer = context.read<SourcePlayback?>()?..addListener(_publish);
    _sourceController = context.read<SourceController?>();
    _sourceController?.addListener(_audiobookCatalogChanged);
    for (final source in [
      context.read<MultiServerProvider?>(),
      context.read<LibrariesProvider?>(),
      context.read<DownloadProvider?>(),
      context.read<HiddenLibrariesProvider?>(),
    ].whereType<Listenable>()) {
      _catalogSources.add(source);
      source.addListener(_catalogChanged);
    }
    _catalogChanged();
    _profiles.addListener(_drivingOwnerChanged);
    WidgetsBinding.instance.addObserver(this);
    _channel.setMethodCallHandler(_handle);
    unawaited(_send('ready').then((_) => _publish()));
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final experience = Provider.of<MusicExperience?>(context);
    if (!identical(experience, _experience)) {
      _experience?.pins.removeListener(_pinsChanged);
      _experience = experience;
      experience?.pins.addListener(_pinsChanged);
    }
    final routes = experience?.drivingRoutes;
    if (identical(routes, _drivingRoutes)) return;
    _drivingRoutes?.removeListener(_bluetoothRouteChanged);
    _routeMonitor?.removeListener(_bluetoothRouteChanged);
    _routeMonitor?.dispose();
    _routeMonitor = null;
    _bluetoothConnected = false;
    _drivingRoutes = routes;
    if (routes != null) {
      routes.addListener(_bluetoothRouteChanged);
      _routeMonitor = MusicSystemAudioRouteMonitor(
        current: () => _current && !_profiles.isBinding && _profiles.lastBindingSucceeded,
      )..addListener(_bluetoothRouteChanged);
    }
  }

  Future<void> _send(String method, [Object? arguments]) async {
    if (!mounted || !_current) return;
    try {
      await _channel.invokeMethod<void>(method, arguments);
    } on MissingPluginException {
      /* No car surface on this platform. */
    } on PlatformException {
      /* Native host can reconnect without affecting audio. */
    }
  }

  void _audiobookCatalogChanged() {
    if (_audiobookRefreshPending || !mounted) return;
    _audiobookRefreshPending = true;
    scheduleMicrotask(() {
      _audiobookRefreshPending = false;
      if (!_current) return;
      _audiobookEntries.clear();
      _offlineSpoken.clear();
      _carArtwork.clear();
      unawaited(_send('catalogChanged'));
    });
  }

  void _catalogChanged() {
    _ownedByServer.clear();
    if (_catalogPending || !_current) return;
    _catalogPending = true;
    scheduleMicrotask(() {
      _catalogPending = false;
      if (!_current) return;
      final clients = context.read<MultiServerProvider?>()?.serverManager.visibleOnlineClients.values;
      final hidden = context.read<HiddenLibrariesProvider?>()?.hiddenLibraryKeys.toList() ?? <String>[];
      hidden.sort();
      final shape = jsonEncode([
        [
          for (final client in clients ?? <MediaServerClient>[])
            [
              client.serverId.value,
              client.serverName,
              identityHashCode(client.authenticationSessionId),
              musicAccountScope(client),
            ],
        ],
        context.read<LibrariesProvider?>()?.libraries.map((library) => [library.globalKey, library.hidden]).toList(),
        context.read<DownloadProvider?>()?.downloadedTracks.map((track) => track.globalKey).toList(),
        hidden,
      ]);
      if (_catalogShape == shape) return;
      if (_video case final session?) {
        if (!session.authorized()) unawaited(_stopCarVideo());
      }
      _catalogShape = shape;
      _downloadGeneration++;
      _ownedByServer.clear();
      _serverRadios.clear();
      _artworkItems.clear();
      _carArtwork.clear();
      unawaited(_send('catalogChanged'));
    });
  }

  Object? _ratingIdentity;
  ({String kind, Object? value, bool canUndo})? _ratingState;
  void _readRating(MediaItem? track, MediaServerClient? client) {
    final identity = (
      track?.globalKey,
      track?.isFavorite,
      track?.userRating,
      client?.authenticationSessionId,
      _profiles.identityMutationGeneration,
    );
    if (_ratingIdentity == identity) return;
    _ratingIdentity = identity;
    _ratingState = null;
    final journal = context.read<MusicExperience?>()?.ratings;
    if (track == null || journal == null) return;
    unawaited(
      journal
          .carState(track)
          .then((value) {
            if (!_current || _ratingIdentity != identity) return;
            _ratingState = value;
            _publishedStatus = null;
            _publish();
          })
          .catchError((Object _) {}),
    );
  }

  void _publish() {
    if (!mounted || !_current) return;
    if (_video != null && (_music.isPlaying || _spoken?.playing == true)) unawaited(_stopCarVideo());
    final queue = _music.queue;
    // Production snapshots change only when occurrences/order change. Status
    // updates must not decode a whole file-backed queue, and duplicate rows
    // must still retire old native positions after a reorder.
    final shape = (
      _spoken?.entry?.key,
      _spoken?.progress?.session,
      _spoken?.spokenQueueRevision,
      _music.queueSessionRevision,
      _music.currentIndex,
      queue is MusicQueueView ? queue : jsonEncode(queue.map((item) => item.globalKey).toList()),
    );
    final changed = _queueShape != shape;
    if (changed) {
      _queueShape = shape;
      _catalogRevision++;
    }
    final spoken = _spoken;
    final track = _music.currentTrack;
    final client = track == null ? null : context.getMediaClientForItemOrNull(track);
    _readRating(track, client);
    final actions = <String, String>{
      if (spoken != null) ...{
        'back15': t.fileSources.skipBack,
        'forward30': t.fileSources.skipForward,
        'speed': '${t.fileSources.speed} · ${spoken.speed}×',
        'bookmark': t.fileSources.bookmark,
      } else if (track != null) ...{
        'shuffle': t.common.shuffle,
        'repeat': switch (_music.repeatMode) {
          MusicRepeatMode.all => t.music.repeatAll,
          MusicRepeatMode.one => t.music.repeatOne,
          _ => t.music.repeat,
        },
        if (client?.capabilities.instantMix == true) 'radio': t.music.instantMix,
        if (_ratingState?.kind == 'favorite') 'favorite': t.libraries.filterCategories.favorites,
        if (_ratingState?.kind == 'rating') 'rating': t.mediaMenu.rate,
      },
    };
    final status = (
      _catalogRevision,
      spoken?.playing ?? _music.isPlaying,
      track?.displayTitle,
      spoken?.entry?.displayTitle,
      _music.shuffled,
      spoken?.chapters.length,
      jsonEncode(actions),
      _ratingState,
      track?.trackArtistTitle,
    );
    if (_publishedStatus == status && DateTime.now().difference(_lastStatus) < const Duration(minutes: 5)) return;
    _lastStatus = DateTime.now();
    _publishedStatus = status;
    if (changed) {
      final start = _music.currentIndex.clamp(0, queue.length);
      final revision = _catalogRevision;
      unawaited(() async {
        final spokenRows = await spoken?.spokenQueue();
        if (!_current || revision != _catalogRevision) return;
        await _send('queueWindow', {
          'scope': _itemScope,
          'revision': revision,
          'title': t.music.queue,
          'rows': [
            if (spoken != null && spoken.entry != null)
              {'index': -1, 'id': 'resume', 'title': spoken.entry!.displayTitle, 'artist': spoken.entry!.series},
            if (spokenRows != null)
              for (final (index, row) in spokenRows.take(49).indexed)
                {
                  'index': index,
                  'id': 'spokenQueue:$_itemScope:${row.id}',
                  'title': row.item.displayTitle,
                  'artist': row.item.series,
                },
            if (spoken == null)
              for (var i = start; i < queue.length && i < start + 50; i++)
                {'index': i, 'title': queue[i].displayTitle, 'artist': queue[i].trackArtistTitle ?? ''},
          ],
        });
      }());
    }
    unawaited(
      _send('snapshot', {
        'scope': _itemScope,
        'labels': {
          'play': t.common.play,
          'pause': t.common.pause,
          'previous': t.music.previousTrack,
          'next': t.music.nextTrack,
        },
        'revision': _catalogRevision,
        'actions': actions,
        'ratingState': [_ratingState?.kind, _ratingState?.value, _ratingState?.canUndo],
        'noMatch': t.messages.noResultsFound,
        'hasQueue': queue.isNotEmpty,
        'hasChapters': spoken?.chapters.isNotEmpty == true,
        'carLabels': {
          'remove': t.musicExperience.remove,
          'search': t.common.search,
          'playNow': t.universalMedia.playNow,
          'next': t.music.playNext,
          'later': t.music.addToQueue,
          'upNext': t.music.upNext,
          'queue': t.music.queue,
          'chapters': t.fileSources.chapters,
          'unlock': t.karaokeUi.unlock,
          'loading': t.common.loading,
          'more': t.fileSources.loadMore,
          'close': t.common.close,
          'retry': t.common.retry,
          'empty': t.messages.noResultsFound,
          'unavailable': t.fileSources.errorUnavailable,
        },
        'favorite': _ratingState?.kind == 'favorite' && _ratingState?.value == true,
        'shuffled': _music.shuffled,
        'repeatMode': _music.repeatMode.name,
        'spoken': spoken != null,
        'playing': spoken?.playing ?? _music.isPlaying,
        'index': _music.currentIndex,
        'title': spoken?.entry?.displayTitle ?? _music.currentTrack?.displayTitle,
        'artist': spoken?.entry?.author ?? _music.currentTrack?.trackArtistTitle,
      }),
    );
  }

  @override
  void dispose() {
    ++_videoIntent;
    unawaited(_stopCarVideo());
    _experience?.pins.removeListener(_pinsChanged);
    WidgetsBinding.instance.removeObserver(this);
    _drivingRoutes?.removeListener(_bluetoothRouteChanged);
    _profiles.removeListener(_drivingOwnerChanged);
    _routeMonitor?.removeListener(_bluetoothRouteChanged);
    _routeMonitor?.dispose();
    final automatic = _automaticDrivingRoute;
    if (automatic != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        automatic.navigator?.removeRoute(automatic);
      });
    }
    _music.removeListener(_publish);
    _spokenPlayer?.removeListener(_publish);
    _sourceController?.removeListener(_audiobookCatalogChanged);
    _sourceController = null;
    _audiobookEntries.clear();
    _offlineSpoken.clear();
    for (final source in _catalogSources) {
      source.removeListener(_catalogChanged);
    }
    _catalogSources.clear();
    if (identical(_owner, _identity)) {
      // The profile may already have changed. Clear the old native catalog
      // even though normal sends are now rejected by the profile guard.
      unawaited(_closeNativeCatalog());
      _channel.setMethodCallHandler(null);
      _owner = null;
      MusicBackgroundFrames.carConnected = false;
    }
    _carArtwork.clear();
    _artworkItems.clear();
    _artworkHttp.close();
    _items.clear();
    _serverRadios.clear();
    _authorizations.clear();
    super.dispose();
  }

  Future<void> _closeNativeCatalog() async {
    try {
      await _channel.invokeMethod<void>('close');
    } on MissingPluginException {
      // No native car surface.
    } on PlatformException {
      // A detached native host has no catalog to clear.
    }
  }

  Map<String, Object?> _folder(String id, String title) => {'id': id, 'title': title, 'playable': false};
  Map<String, Object?> _item(MediaItem item, {bool offline = false, ({MediaItem item, int index})? parent}) {
    final id = 'item:$_itemScope:${++_itemSequence}';
    // Bounded lookup table also invalidates stale native rows after a profile switch.
    if (_items.length >= 2000 && !_items.containsKey(id)) {
      final first = _items.keys.first;
      _items.remove(first);
      _parents.remove(first);
      _playlistRevisions.remove(first);
      _offlineRows.remove(first);
      _authorizations.remove(first);
    }
    _items[id] = item;
    if (parent != null) _parents[id] = parent;
    if (offline) _offlineRows.add(id);
    _rememberArtwork(id, item);
    final client = context.getMediaClientForItemOrNull(item);
    _authorizations[id] = (
      client: client,
      authentication: client?.authenticationSessionId,
      account: client == null ? null : musicAccountScope(client),
      generation: _profiles.identityMutationGeneration,
    );
    return {
      'id': id,
      'title': item.displayTitle,
      'subtitle': item.trackArtistTitle ?? item.albumTitle ?? '',
      'playable': item.kind == MediaKind.track,
      'kind': item.kind.name,
      'artwork': true,
    };
  }

  void _rememberArtwork(String id, MediaItem item) {
    final client = context.getMediaClientForItemOrNull(item);
    if (_artworkItems.length >= 2000 && !_artworkItems.containsKey(id)) {
      _artworkItems.remove(_artworkItems.keys.first);
    }
    _artworkItems[id] = (
      item: item,
      client: client,
      authentication: client?.authenticationSessionId,
      scope: client == null ? null : musicAccountScope(client),
    );
  }

  Map<String, Object?> _queueItem(MediaItem item, int index) {
    final id = 'queue:$_itemScope:$_catalogRevision:$index';
    _rememberArtwork(id, item);
    return {
      'id': id,
      'title': item.displayTitle,
      'subtitle': item.trackArtistTitle ?? '',
      'playable': true,
      'artwork': true,
    };
  }

  Map<String, Object?> _audiobookItem(SourceEntry entry, {bool offline = false}) {
    final id = 'audiobook:$_itemScope:${++_audiobookSequence}';
    if (_audiobookEntries.length >= 2000) {
      _offlineSpoken.remove(_audiobookEntries.keys.first);
      _audiobookEntries.remove(_audiobookEntries.keys.first);
    }
    _audiobookEntries[id] = entry;
    if (offline) _offlineSpoken.add(id);
    return {
      'id': id,
      'title': entry.displayTitle,
      'subtitle': entry.narrator.isNotEmpty ? entry.narrator : entry.author,
      'kind': _isEpisode(entry) ? 'podcast' : 'audiobook',
      'playable': true,
      'artwork': true,
    };
  }

  bool Function() _readClient(MediaServerClient client) {
    final authentication = client.authenticationSessionId, account = musicAccountScope(client);
    final generation = _profiles.identityMutationGeneration;
    return () =>
        _current &&
        generation == _profiles.identityMutationGeneration &&
        _clientCurrent(client, authentication) &&
        account == musicAccountScope(client) &&
        !client.isOfflineMode &&
        context.read<MultiServerProvider>().serverManager.isClientOnline(client.serverId);
  }

  bool _clientCurrent(MediaServerClient client, Object authentication) {
    if (!_current) return false;
    final manager = context.read<MultiServerProvider>().serverManager;
    return manager.isServerVisible(client.serverId) &&
        identical(manager.getClient(client.serverId), client) &&
        identical(authentication, client.authenticationSessionId);
  }

  Future<bool> _carTransport(String command) async {
    final sourcePlayback = context.read<SourcePlayback?>();
    if (sourcePlayback != null && sourcePlayback.active && !sourcePlayback.video && _isSpoken(sourcePlayback.entry)) {
      switch (command) {
        case 'back15':
          await sourcePlayback.skip(-15);
          return true;
        case 'forward30':
          await sourcePlayback.skip(30);
          return true;
        case 'bookmark':
          await sourcePlayback.bookmark();
          await _send('catalogChanged');
          return true;
        case 'speed':
          const speeds = [1.0, 1.25, 1.5, 1.75, 2.0];
          await sourcePlayback.setSpeed(speeds.where((s) => s > sourcePlayback.speed).firstOrNull ?? 1);
          return true;
        case 'play':
          await sourcePlayback.play();
          return sourcePlayback.playing;
        case 'pause':
          await sourcePlayback.pause();
          return true;
        case 'previous':
          await sourcePlayback.previousChapter();
          return true;
        case 'next':
          await sourcePlayback.nextChapter();
          return true;
      }
    }
    if (!mounted || !_current) return false;
    switch (command) {
      case 'shuffle':
        _music.toggleShuffle();
        return true;
      case 'repeat':
        final modes = MusicRepeatMode.values;
        _music.setRepeatMode(modes[(_music.repeatMode.index + 1) % modes.length]);
        return true;
      case 'radio':
        final track = _music.currentTrack;
        if (track == null || context.getMediaClientForItemOrNull(track)?.capabilities.instantMix != true) return false;
        return await _music.playInstantMix(track) == InstantMixOutcome.started;
      case 'favorite':
        final track = _music.currentTrack, journal = context.read<MusicExperience?>()?.ratings;
        if (track == null || journal == null || _ratingState?.kind != 'favorite') return false;
        final result = await journal.carEdit(track, 'favorite');
        if (_current) {
          _ratingIdentity = null;
          _publish();
        }
        return result;
      case 'play':
        await _music.play();
        return _music.isPlaying;
      case 'pause':
        await _music.pause();
        return true;
      case 'previous':
        await _music.previous();
        return true;
      case 'next':
        await _music.next();
        return true;
      default:
        throw PlatformException(code: 'invalid_transport');
    }
  }

  String _voiceName(String text) => text
      .toLowerCase()
      .replaceAll(RegExp(r"['’]"), '')
      .replaceAll(RegExp(r'[^\p{L}\p{N}]+', unicode: true), ' ')
      .trim();

  Future<List<MediaItem>> _visibleDownloads() async {
    final downloads = context.read<DownloadProvider?>();
    final hidden = context.read<HiddenLibrariesProvider?>();
    await hidden?.ensureInitialized();
    if (!mounted || !_current || downloads == null || downloads.isDisposed || downloads.activeProfileId != _profileId) {
      return [];
    }
    final denied = _hiddenCarLibraries(), manager = context.read<MultiServerProvider?>()?.serverManager;
    return downloads.downloadedTracks
        .where(
          (track) =>
              track.serverId != null &&
              manager?.isServerVisible(ServerId(track.serverId!)) != false &&
              !denied.contains('${track.serverId}:${track.libraryId}'),
        )
        .toList();
  }

  Future<bool> _playGenre(String query, {required Future<bool> Function() commit}) async {
    final name = _voiceName(query);
    if (name.isEmpty || name.length > 200) return false;
    final intent = _music.beginPlayIntent(), generation = _profiles.identityMutationGeneration;
    bool current() =>
        _current && generation == _profiles.identityMutationGeneration && _music.isPlayIntentCurrent(intent);
    final hidden = context.read<HiddenLibrariesProvider?>();
    final libraries = context.read<LibrariesProvider?>();
    final downloads = context.read<DownloadProvider?>();
    final servers = context.read<MultiServerProvider?>();
    await hidden?.ensureInitialized();
    if (!current()) return false;
    final batches = await Future.wait(
      (libraries?.libraries ?? [])
          .where(
            (library) =>
                library.kind == MediaKind.artist &&
                !library.hidden &&
                library.serverId != null &&
                hidden?.isLibraryHidden(library.globalKey) != true,
          )
          .take(16)
          .map((library) async {
            final id = ServerId(library.serverId!);
            final client = servers?.getClientForServer(id);
            if (client == null || client.backend != library.backend || client.isOfflineMode) return null;
            final owner = _readClient(client);
            bool authorized() =>
                current() &&
                owner() &&
                hidden?.isLibraryHidden(library.globalKey) != true &&
                libraries!.libraries.any(
                  (row) => row.globalKey == library.globalKey && row.backend == client.backend && !row.hidden,
                );
            if (!authorized()) return null;
            try {
              final result = await client.fetchLibraryFiltersWithValues(library.id, libraryKind: MediaKind.track);
              if (!mounted || !authorized()) return null;
              for (final filter in result.filters.where(
                (filter) => filter.filter.split('.').last.toLowerCase() == 'genre' && filter.hasValueList,
              )) {
                if (!mounted || !authorized()) return null;
                final values =
                    result.cachedValues[filter.filter] ??
                    await context.tryGetPlexClientForServer(id)?.getFilterValues(filter.key) ??
                    [];
                if (!authorized()) return null;
                final ids = values
                    .where((v) => _voiceName(v.title) == name)
                    .map((v) => libraryFilterValueId(v.key, filter.filter))
                    .toSet();
                if (ids.isEmpty) continue;
                return await MusicStation(
                  id: 'voiceGenre',
                  title: query,
                  serverId: id.value,
                  libraryId: library.id,
                  filters: [LibraryFilter(field: filter.filter, values: ids.toList())],
                ).preview(client, current: authorized);
              }
            } on Exception {
              // Other libraries and verified local tracks can still answer this request.
            }
            return null;
          }),
    );
    if (!current()) return false;
    final online = MusicDiscovery.interleave([
      for (final batch in batches.whereType<MusicStationBatch>())
        if (batch.isCurrent()) batch.tracks,
    ], limit: 200);
    final local = downloads == null || downloads.activeProfileId != _profileId
        ? <MediaItem>[]
        : downloads.downloadedTracks
              .where(
                (track) =>
                    hidden?.isLibraryHidden('${track.serverId}:${track.libraryId}') != true &&
                    track.genres?.any((genre) => _voiceName(genre) == name) == true,
              )
              .take(200)
              .toList();
    if (online.isEmpty && local.isEmpty || !await commit() || !current()) return false;
    await _music.playFromList(
      tracks: online.isEmpty ? local : online,
      offline: online.isEmpty,
      shuffle: true,
      playContext: MusicPlayContext(title: query.trim(), kind: MusicPlayContextKind.mix),
    );
    return _current && _music.isPlaying;
  }

  Future<Object?> _handle(MethodCall call) async {
    if (!mounted || !_current || _profileId == null) throw PlatformException(code: 'profile_unavailable');
    if (call.method == 'playItem' || call.method == 'playSearch' || call.method == 'jump') {
      _playlistGeneration++;
    }
    final playlistGeneration = _playlistGeneration;
    final requestGeneration = _profiles.identityMutationGeneration;
    bool requestCurrent() => _current && requestGeneration == _profiles.identityMutationGeneration;
    final args = call.arguments is Map ? Map<String, dynamic>.from(call.arguments as Map) : <String, dynamic>{};
    var accepted = false;
    Future<bool> commit() async {
      if (!requestCurrent()) return false;
      if (accepted || args['carRequest'] == null) return true;
      accepted = await _channel.invokeMethod<bool>('acceptCarRequest', args['carRequest']) == true;
      return accepted && requestCurrent();
    }

    switch (call.method) {
      case 'transport':
        if (args['scope'] != _itemScope || args['revision'] != _catalogRevision) {
          throw PlatformException(code: 'stale_widget');
        }
        if (!await commit()) return false;
        return _carTransport(args['command'] as String? ?? '');
      case 'videoEvent':
        return _videoCommands.run(() => _carVideoEvent(args));
      case 'videoStart':
        final intent = ++_videoIntent;
        return _videoCommands.run(() => _startCarVideo(args['id'] as String? ?? '', intent, commit));
      case 'guestAction':
        final room = context.read<GuestJukebox?>();
        if (room == null || !_connected || args['scope'] != _itemScope) return false;
        final action = args['action'];
        if (action == 'invite') return room.link?.toString();
        if (action == 'close') {
          if (!await commit()) return false;
          room.close();
          return true;
        }
        if (action != 'open') return false;
        final hidden = context.read<HiddenLibrariesProvider>();
        await hidden.ensureInitialized();
        if (!mounted || !requestCurrent() || !_connected) return false;
        final library = context
            .read<LibrariesProvider>()
            .libraries
            .where(
              (l) =>
                  l.globalKey == args['library'] &&
                  l.kind.isMusic &&
                  !l.hidden &&
                  !hidden.hiddenLibraryKeys.contains(l.globalKey),
            )
            .firstOrNull;
        final network = args['network'] as String?;
        if (library == null ||
            network == null ||
            !(await KaraokeRoomServer.addresses()).contains(network) ||
            !await commit() ||
            !_connected) {
          return false;
        }
        await room.open(library, address: network);
        if (!requestCurrent() || !_connected || hidden.isLibraryHidden(library.globalKey)) {
          room.close();
          return false;
        }
        return room.link != null;
      case 'artwork':
        return _artwork(args);
      case 'browse':
        return _browse(args['id'] as String? ?? 'root');
      case 'search':
        final query = args['query'] as String? ?? '';
        final (results, books) = await (_search(query), _searchAudiobooks(query)).wait;
        final owned = {for (final row in await _visibleDownloads()) (row.backend, row.globalKey)};
        final denied = requestCurrent() ? _hiddenCarLibraries() : <String>{};
        return requestCurrent()
            ? [
                for (final row in results)
                  if (!denied.contains('${row.serverId}:${row.libraryId}'))
                    _item(row, offline: owned.contains((row.backend, row.globalKey))),
                ...books,
              ]
            : [];
      case 'resolveVoice':
        final title = _voiceName(args['query'] as String? ?? '');
        final artist = _voiceName(args['artist'] as String? ?? '');
        final album = _voiceName(args['album'] as String? ?? '');
        final kind = args['kind'] as String?;
        if (title.isEmpty || title.length > 200) return [];
        const latestPrefix = 'latest unplayed episode of ';
        final raw = (args['query'] as String? ?? '').trim();
        final englishLatest =
            LocaleSettings.currentLocale.languageCode == 'en' && raw.toLowerCase().startsWith(latestPrefix);
        if (kind == 'show' || englishLatest && (kind == null || kind == 'podcast') && artist.isEmpty && album.isEmpty) {
          final showName = englishLatest ? raw.substring(latestPrefix.length).trim() : raw;
          final sources = _sourceController;
          if (showName.isEmpty || sources == null || sources.blocked || sources.profileId != _profileId) return [];
          final epoch = sources.generation;
          bool current() => mounted && requestCurrent() && sources.current(epoch);
          final shows = await sources.repository.podcastShows(
            _profileId,
            title: showName,
            sourceIds: {for (final source in sources.sources) source.id},
            limit: 10,
          );
          final choices = <Map<String, Object?>>[];
          for (final show in shows) {
            if (!current()) return [];
            if (!sources.sources.any((s) => s.id == show.source)) continue;
            final episode = await sources.repository.podcastEpisodeForShow(
              _profileId,
              source: show.source,
              parent: show.parent,
              latestUnplayed: englishLatest || args['latest'] == true,
              current: current,
            );
            if (!current()) return [];
            if (episode != null && sources.sources.any((s) => s.id == show.source)) {
              choices.add(_audiobookItem(episode));
            }
          }
          return current() ? choices : [];
        }
        if (kind == 'genre') {
          final query = args['query'] as String;
          return [
            {
              'id': 'genre:$_itemScope:${base64Url.encode(utf8.encode(query))}',
              'title': query,
              'kind': 'station',
              'playable': true,
            },
          ];
        }
        if (artist.isEmpty && album.isEmpty) {
          final english = LocaleSettings.currentLocale.languageCode == 'en';
          if ((title == _voiceName(t.navigation.downloads) ||
                  english && {'my downloads', 'downloaded music', 'shuffle downloads'}.contains(title)) &&
              (await _visibleDownloads()).isNotEmpty &&
              requestCurrent()) {
            return [
              {'id': 'downloadMix:$_itemScope', 'title': t.navigation.downloads, 'kind': 'playlist', 'playable': true},
            ];
          }
          if (!mounted || !requestCurrent()) return [];
          if ((args['similar'] == true || english && {'more like this', 'play more like this'}.contains(title)) &&
              _music.currentTrack != null &&
              context.getMediaClientForItemOrNull(_music.currentTrack!)?.capabilities.instantMix == true) {
            return [
              {
                'id': 'similar:$_itemScope:$_catalogRevision',
                'title': t.music.instantMix,
                'kind': 'station',
                'playable': true,
              },
            ];
          }
        }
        if ((kind == null || kind == 'playlist') &&
            artist.isEmpty &&
            album.isEmpty &&
            (title == _voiceName(t.libraries.filterCategories.favorites) ||
                (LocaleSettings.currentLocale.languageCode == 'en' &&
                    const {'favorites', 'favourites', 'my favorites', 'my favourites'}.contains(title)))) {
          if (!mounted || !requestCurrent()) return [];
          final selection = await loadProfileMusicFavorites(context);
          if (requestCurrent() && selection.tracks.isNotEmpty) {
            return [
              {
                'id': 'favorites:$_itemScope',
                'title': t.libraries.filterCategories.favorites,
                'kind': 'playlist',
                'playable': true,
              },
            ];
          }
        }
        final (results, audiobookMatches) = await (
          _search(args['query'] as String? ?? ''),
          _searchAudiobooks(args['query'] as String? ?? ''),
        ).wait;
        final owned = {for (final track in await _visibleDownloads()) (track.backend, track.globalKey)};
        if (!mounted || !requestCurrent()) return [];
        final resolved = results
            .where(
              (item) =>
                  _voiceName(item.displayTitle) == title &&
                  (artist.isEmpty ||
                      _voiceName(
                            item.kind == MediaKind.artist
                                ? item.displayTitle
                                : item.trackArtistTitle ?? item.albumArtistTitle ?? '',
                          ) ==
                          artist) &&
                  (album.isEmpty ||
                      _voiceName(item.kind == MediaKind.album ? item.displayTitle : item.albumTitle ?? '') == album) &&
                  (kind == null || item.kind.name == kind),
            )
            .where((item) => !_hiddenCarLibraries().contains('${item.serverId}:${item.libraryId}'))
            .take(10)
            .map((item) => _item(item, offline: owned.contains((item.backend, item.globalKey))))
            .toList();
        for (final row in resolved) {
          if (resolved.where((other) => other['title'] == row['title']).length < 2) continue;
          final source = _authorizations[row['id']]?.client?.serverName;
          if (source != null) {
            row['subtitle'] = [row['subtitle'], source].whereType<String>().where((s) => s.isNotEmpty).join(' · ');
          }
        }
        if (!mounted || !requestCurrent()) return [];
        resolved.addAll(
          audiobookMatches.where(
            (row) =>
                _voiceName(row['title'] as String? ?? '') == title &&
                (artist.isEmpty || _voiceName(row['subtitle'] as String? ?? '') == artist) &&
                (kind == null || kind == row['kind']),
          ),
        );
        if (artist.isEmpty && album.isEmpty && (kind == null || kind == 'station' || kind == 'playlist')) {
          final store = MusicLibraryStore(context.read<AppDatabase>(), _profileId);
          final stations = kind == 'playlist' ? <Map<String, dynamic>>[] : await store.documents('station');
          final recipes = kind == 'station'
              ? <MusicRecipe>[]
              : await MusicRecipeStore(store.database, _profileId).load();
          if (!requestCurrent()) return [];
          resolved.addAll(
            stations
                .where(
                  (row) => row['id'] is String && row['title'] is String && _voiceName(row['title'] as String) == title,
                )
                .map(
                  (row) => {'id': 'station:${row['id']}', 'title': row['title'], 'kind': 'station', 'playable': true},
                ),
          );
          resolved.addAll(
            recipes
                .where((recipe) => _voiceName(recipe.title) == title)
                .map(
                  (recipe) => {
                    'id': 'recipe:${recipe.id}',
                    'title': recipe.title,
                    'kind': 'playlist',
                    'playable': true,
                  },
                ),
          );
        }
        if (artist.isEmpty && album.isEmpty && (kind == null || kind == 'station')) {
          final radios = await _serverRadioRows();
          if (!requestCurrent()) return [];
          resolved.addAll(radios.where((row) => _voiceName(row['title'] as String) == title));
        }
        return requestCurrent() ? resolved.take(10).toList() : [];
      case 'playItem':
        var id = args['id'] as String? ?? '';
        if (id.startsWith('spokenAction:')) {
          final parts = id.split(':');
          if (parts.length < 3 || !['play', 'next', 'later', 'remove'].contains(parts[1])) return false;
          if (parts[1] != 'play') args['queue'] = parts[1];
          id = parts.skip(2).join(':');
          if (!id.startsWith('spokenQueue:$_itemScope:') && !_audiobookEntries.containsKey(id)) return false;
        }
        if (id.startsWith('shuffle:')) {
          id = id.substring(8);
          args['shuffle'] = true;
        }
        if (Platform.isAndroid &&
            id == 'guestPhone:$_itemScope' &&
            (_connected || PlatformDetector.isAutomotiveMedia())) {
          if (!await commit() || !mounted || context.read<GuestJukebox?>() == null) return false;
          _guestPending = true;
          _openGuestWhenAvailable();
          return true;
        }
        if (id == 'downloadMix:$_itemScope') {
          final tracks = await _visibleDownloads();
          if (tracks.isEmpty || !await commit()) return false;
          if (!_enqueueRequested(tracks, args)) {
            await _music.playFromList(
              tracks: tracks,
              offline: true,
              shuffle: true,
              playContext: MusicPlayContext(title: t.navigation.downloads, kind: MusicPlayContextKind.tracks),
            );
          }
          return true;
        }
        if (id.startsWith('score:$_itemScope:$_catalogRevision:')) {
          final track = _music.currentTrack, journal = context.read<MusicExperience?>()?.ratings;
          if (track == null || journal == null || _ratingState?.kind != 'rating' || !await commit()) return false;
          final value = id.split(':').last;
          final result = await journal.carEdit(track, 'rating', rating: double.tryParse(value), undo: value == 'undo');
          if (_current) {
            _ratingIdentity = null;
            _publish();
          }
          return result;
        }
        if (id.startsWith('genre:$_itemScope:')) {
          if (args['queue'] != null) return false;
          return _playGenre(utf8.decode(base64Url.decode(id.split(':').last)), commit: commit);
        }
        if (id == 'similar:$_itemScope:$_catalogRevision') {
          return args['queue'] == null && await commit() && await _carTransport('radio');
        }
        if (id == 'favorites:$_itemScope') {
          final intent = _music.beginPlayIntent();
          final selection = await loadProfileMusicFavorites(context);
          if (!requestCurrent() || !_music.isPlayIntentCurrent(intent)) return false;
          final tracks = selection.tracks;
          if (tracks.isEmpty || !await commit()) return false;
          if (!_enqueueRequested(tracks, args)) {
            await _music.playFromList(
              tracks: tracks,
              shuffle: args['shuffle'] == true,
              playContext: MusicPlayContext(
                title: t.libraries.filterCategories.favorites,
                kind: MusicPlayContextKind.tracks,
              ),
            );
          }
          return true;
        }
        if (id.startsWith('spokenQueue:$_itemScope:')) {
          final playback = context.read<SourcePlayback?>();
          if (playback == null || !await commit()) return false;
          if (args['queue'] == 'remove') {
            final removed = await playback.removeQueuedSpoken(id.split(':').last);
            if (removed) await _send('catalogChanged');
            return removed;
          }
          return playback.playQueuedSpoken(id.split(':').last);
        }
        if (id == 'resume') return await commit() && await _carTransport('play');
        if (id.startsWith('bookmark:')) {
          final spoken = _spoken, source = _sourceController, entry = spoken?.entry, revision = _catalogRevision;
          final prefix = 'bookmark:$_itemScope:$revision:';
          if (!id.startsWith(prefix) ||
              source == null ||
              source.blocked ||
              source.profileId != _profileId ||
              entry == null) {
            return false;
          }
          final notes = await source.repository.annotations(_profileId, entry.key);
          if (!requestCurrent() || source.blocked || _spoken?.entry?.key != entry.key || _catalogRevision != revision) {
            return false;
          }
          final note = notes
              .where(
                (note) =>
                    note.id == id.substring(prefix.length) &&
                    note.mode == PublicationMode.listening &&
                    note.edition == spoken!.progress?.edition &&
                    note.positionMs != null,
              )
              .firstOrNull;
          if (note == null) return false;
          if (!await commit()) return false;
          await spoken!.seekBookmark(note);
          return true;
        }
        if (id.startsWith('chapter:')) {
          final parts = id.split(':');
          final index = int.tryParse(parts.last);
          final spoken = _spoken;
          if (parts.length != 4 ||
              parts[1] != _itemScope ||
              parts[2] != '$_catalogRevision' ||
              spoken == null ||
              index == null ||
              index < 0 ||
              index >= spoken.chapters.length) {
            return false;
          }
          if (!await commit()) return false;
          await spoken.seekChapter(index);
          return true;
        }
        if (id.startsWith('audiobook:')) {
          final entry = _audiobookEntries[id];
          final sources = _sourceController;
          final playback = context.read<SourcePlayback?>();
          if (entry == null || sources == null || playback == null) {
            throw PlatformException(code: 'stale_audiobook');
          }

          if (sources.profileId != _profileId ||
              sources.blocked ||
              !sources.sources.any((source) => source.id == entry.sourceId && source.profileId == _profileId)) {
            throw PlatformException(code: 'stale_audiobook');
          }
          final epoch = sources.generation;
          final verified = await sources.downloads.verifiedFiles(_profileId, [entry]);
          if (!requestCurrent() || !sources.current(epoch)) return false;
          final local = PublicationOfflineReadiness.evaluate(entry, verified).canListen;
          if (_offlineSpoken.contains(id) && !local) return false;
          final fresh = local ? entry : await sources.hydrate(entry);
          if (!mounted || !requestCurrent() || !sources.current(epoch)) return false;
          if (fresh.key != entry.key ||
              sources.blocked ||
              sources.profileId != _profileId ||
              !sources.sources.any((source) => source.id == entry.sourceId) ||
              !_isSpoken(fresh) ||
              fresh.audio.isEmpty) {
            throw PlatformException(code: 'unavailable_audiobook');
          }
          if (!await commit()) return false;
          if (args['queue'] == 'next' || args['queue'] == 'later') {
            await playback.enqueueSpoken(fresh, next: args['queue'] == 'next', offline: _offlineSpoken.contains(id));
            await _send('catalogChanged');
            return requestCurrent();
          }
          await playback.start(fresh, fresh.audio, isStartCurrent: requestCurrent);
          return requestCurrent() && playback.active && playback.entry?.key == fresh.key;
        } else if (id.startsWith('queue:')) {
          final parts = id.split(':');
          if (parts.length != 4 || parts[1] != _itemScope || int.tryParse(parts[2]) != _catalogRevision) {
            throw PlatformException(code: 'stale_queue');
          }
          final index = int.tryParse(parts[3]);
          if (index == null || index < 0 || index >= _music.queue.length) throw PlatformException(code: 'stale_queue');
          if (!await commit()) return false;
          if (!await commit()) return false;
          await _music.jumpTo(index);
        } else if (id.startsWith('serverRadio:')) {
          final entry = _serverRadios[id];
          if (entry == null || !entry.current()) throw PlatformException(code: 'stale_item');
          if (args['queue'] == 'next' || args['queue'] == 'later') {
            final intent = _music.beginPlayIntent();
            final tracks = await (entry.client as ServerMusicRadioClient).serverMusicRadioTracks(entry.radio);
            if (!entry.current() || !_music.isPlayIntentCurrent(intent)) return false;
            if (tracks.any(
              (track) =>
                  track.kind != MediaKind.track ||
                  track.id.isEmpty ||
                  track.backend != entry.client.backend ||
                  track.serverId != entry.radio.serverId ||
                  track.libraryId != entry.radio.libraryId,
            )) {
              throw const FormatException('Foreign music radio track');
            }
            return tracks.isNotEmpty && await commit() && _enqueueRequested(tracks, args);
          }
          if (_music is! MusicServerRadioControls) throw PlatformException(code: 'unsupported_server_radio');
          if (!await commit()) return false;
          final outcome = await (_music as MusicServerRadioControls).playServerRadio(
            entry.radio,
            shuffle: args['shuffle'] == true,
          );
          if (outcome != InstantMixOutcome.started) return false;
        } else if (id.startsWith('station:') || id.startsWith('offlineStation:')) {
          final generation = _profiles.identityMutationGeneration;
          bool stationCurrent() => _current && generation == _profiles.identityMutationGeneration;
          final intent = _music.beginPlayIntent();
          final store = MusicLibraryStore(context.read<AppDatabase>(), _profileId);
          final row = await store.document('station', id.substring(id.indexOf(':') + 1));
          if (!mounted || !stationCurrent() || row == null) return false;
          final station = MusicStation.fromJson(row);
          final hidden = context.read<HiddenLibrariesProvider>();
          await hidden.ensureInitialized();
          if (!mounted || !_current || hidden.hiddenLibraryKeys.contains('${station.serverId}:${station.libraryId}')) {
            return false;
          }
          final client = context.read<MultiServerProvider>().getClientForServer(ServerId(station.serverId));
          final offline = client == null || client.isOfflineMode || id.startsWith('offlineStation:');
          final authentication = client?.authenticationSessionId;
          final accountScope = client == null ? null : musicAccountScope(client);
          final downloads = offline ? context.read<DownloadProvider>() : null;
          var generating = true;
          final batch = offline
              ? null
              : await station.preview(
                  client,
                  current: () =>
                      stationCurrent() &&
                      (!generating || _music.isPlayIntentCurrent(intent)) &&
                      _clientCurrent(client, authentication!) &&
                      !hidden.hiddenLibraryKeys.contains('${station.serverId}:${station.libraryId}'),
                );
          generating = false;
          final tracks = offline
              ? musicSnapshotTracks(
                  await store.document('stationSnapshot', station.id),
                  downloads!.downloadedTracks,
                  accountScope: accountScope,
                ).where((track) => track.serverId == station.serverId && track.libraryId == station.libraryId).toList()
              : batch!.tracks;

          if (!stationCurrent() ||
              hidden.hiddenLibraryKeys.contains('${station.serverId}:${station.libraryId}') ||
              batch != null && !batch.isCurrent() ||
              !_music.isPlayIntentCurrent(intent) ||
              client != null && !_clientCurrent(client, authentication!) ||
              client != null && accountScope != musicAccountScope(client) ||
              tracks.isEmpty) {
            return false;
          }
          if (!await commit()) return false;
          if (_enqueueRequested(tracks, args)) return true;
          if (!offline && station.continuous) {
            if (_music is! MusicStationRadioControls || station.albums && args['shuffle'] == true) {
              throw PlatformException(code: 'unsupported_station_radio');
            }
            final outcome = await (_music as MusicStationRadioControls).playStationRadio(
              batch!,
              shuffle: args['shuffle'] == true,
            );
            if (outcome != InstantMixOutcome.started) return false;
          } else {
            await _music.playFromList(
              tracks: tracks,
              playContext: MusicPlayContext(title: station.title, kind: MusicPlayContextKind.mix),
              shuffle: args['shuffle'] == true,
              offline: offline,
            );
          }
        } else if (id.startsWith('recipe:')) {
          final generation = _profiles.identityMutationGeneration;
          bool recipeCurrent() => _current && _profiles.identityMutationGeneration == generation;
          final recipes = await MusicRecipeStore(context.read<AppDatabase>(), _profileId).load();
          if (!mounted || !recipeCurrent()) return false;
          final matching = recipes.where((recipe) => recipe.id == id.substring(7)).toList();
          if (matching.length != 1) throw PlatformException(code: 'stale_item');
          final intent = _music.beginPlayIntent();
          final hidden = context.read<HiddenLibrariesProvider?>();
          final servers = context.read<MultiServerProvider>();
          await hidden?.ensureInitialized();
          var generating = true;
          final batch = await MusicDiscovery((item) {
            final server = serverIdOrNull(item.serverId);
            if (!recipeCurrent() ||
                generating && !_music.isPlayIntentCurrent(intent) ||
                server == null ||
                !servers.isServerOnline(server) ||
                (item.libraryId != null && hidden?.isLibraryHidden('$server:${item.libraryId}') == true)) {
              return null;
            }
            return servers.getClientForServer(server);
          }).generate(matching.single);
          generating = false;
          if (!_music.isPlayIntentCurrent(intent) || !batch.isCurrent()) return false;
          final tracks = batch.tracks;
          if (!await commit()) return false;
          if (_enqueueRequested(tracks, args)) return true;
          if (matching.single.kind == MusicRecipeKind.albumRadio) {
            if (_music is! MusicAlbumRadioControls || args['shuffle'] == true) {
              throw PlatformException(code: 'unsupported_album_radio');
            }
            final outcome = await (_music as MusicAlbumRadioControls).playAlbumRadio(
              matching.single.seeds.single,
              maxDistance: matching.single.maxDistance,
              preview: batch,
            );
            if (outcome != InstantMixOutcome.started) return false;
          } else {
            await _music.playFromList(
              tracks: tracks,
              playContext: MusicPlayContext(title: matching.single.title, kind: MusicPlayContextKind.mix),
              shuffle: args['shuffle'] == true,
            );
          }
        } else {
          final selected = _items[id];
          final parent = args['queue'] == null ? _parents[id] : null;
          final item = parent?.item ?? selected;
          if (item == null || selected == null) throw PlatformException(code: 'stale_item');
          final intent = _music.beginPlayIntent();
          final hidden = context.read<HiddenLibrariesProvider?>();
          await hidden?.ensureInitialized();
          if (!mounted ||
              !_current ||
              !_music.isPlayIntentCurrent(intent) ||
              hidden?.hiddenLibraryKeys.contains('${item.serverId}:${item.libraryId}') == true) {
            return false;
          }
          if (!_catalogItemCurrent(id)) return false;
          final client = _authorizations[id]!.client;
          List<MediaItem>? playlistTracks;
          if (_offlineRows.contains(id) && item.kind == MediaKind.playlist) {
            final store = MusicLibraryStore(context.read<AppDatabase>(), _profileId);
            playlistTracks = await store.ownedPlaylistTracks(
              item,
              await _visibleDownloads(),
              client == null ? null : musicAccountScope(client),
              revision: _playlistRevisions[id],
            );
            if (!mounted || !_catalogItemCurrent(id) || playlistTracks.isEmpty) return false;
          }
          final scope = await MusicReadScope.forItem(
            context,
            item,
            selectedClient: client,
            offlinePlaylistTracks: playlistTracks,
            offline: _offlineRows.contains(id) || client == null || client.isOfflineMode,
          );
          bool current() => _catalogItemCurrent(id) && _music.isPlayIntentCurrent(intent) && scope?.current == true;
          if (!current()) return false;
          if (!scope!.offline && item.kind == MediaKind.playlist) {
            final started = await _playOnlinePlaylist(
              item: item,
              selected: selected,
              start: parent?.index,
              scope: scope,
              args: args,
              sourceCurrent: () =>
                  _playlistGeneration == playlistGeneration && _catalogItemCurrent(id) && scope.current,
              intent: intent,
              commit: commit,
            );
            if (started && args['queue'] == null) {
              if (args['repeat'] == 'one') _music.setRepeatMode(MusicRepeatMode.one);
              if (args['repeat'] == 'all') _music.setRepeatMode(MusicRepeatMode.all);
            }
            return started;
          }
          final playable = scope.offline
              ? scope.offlineTracks
              : item.kind == MediaKind.track
              ? [item]
              : item.kind == MediaKind.album || item.kind == MediaKind.artist
              ? await fetchMusicTracks(
                  client!,
                  item.id,
                  kind: item.kind,
                  libraryId: item.libraryId,
                  current: current,
                  acceptsItem: scope.ownsTrack,
                )
              : <MediaItem>[];
          if (!current() || playable.isEmpty) return false;
          final start = parent?.index;
          if (start != null &&
              (start < 0 ||
                  start >= playable.length ||
                  playable[start].globalKey != selected.globalKey ||
                  playable[start].backend != selected.backend ||
                  playable[start].playlistItemId != selected.playlistItemId)) {
            return false;
          }
          if (!await commit() || !current()) return false;
          if (args['queue'] == 'next') {
            _music.addNext(playable);
            return true;
          }
          if (args['queue'] == 'later') {
            _music.addToEnd(playable);
            return true;
          }
          final playContext = MusicPlayContext(
            title: item.displayTitle,
            kind: item.kind == MediaKind.album
                ? MusicPlayContextKind.album
                : item.kind == MediaKind.artist
                ? MusicPlayContextKind.artist
                : item.kind == MediaKind.playlist
                ? MusicPlayContextKind.playlist
                : MusicPlayContextKind.tracks,
          );
          if (start != null) {
            await _music.playFromOccurrence(
              tracks: playable,
              index: start,
              playContext: playContext,
              offline: scope.offline,
            );
          } else {
            await _music.playFromList(
              tracks: playable,
              playContext: playContext,
              offline: scope.offline,
              shuffle: args['shuffle'] == true,
            );
          }
        }
        if (args['repeat'] == 'one') _music.setRepeatMode(MusicRepeatMode.one);
        if (args['repeat'] == 'all') _music.setRepeatMode(MusicRepeatMode.all);
        return _music.isPlaying;
      case 'playSearch':
        if (args['kind'] == 'genre') return _playGenre(args['query'] as String? ?? '', commit: commit);
        if ((args['query'] as String? ?? '').trim().isEmpty) {
          if (_spoken != null || _music.currentTrack != null) return await commit() && await _carTransport('play');
          final recent = await _browse('recent');
          if (!requestCurrent() || recent.isEmpty) return false;
          return _handle(MethodCall('playItem', {...args, 'id': recent.first['id']}));
        }
        final intent = _music.beginPlayIntent();
        final matches = await _handle(MethodCall('resolveVoice', args)) as List;
        if (!mounted || !_current || !_music.isPlayIntentCurrent(intent)) return false;
        if (matches.isEmpty) throw PlatformException(code: 'no_match');
        if (matches.length != 1) throw PlatformException(code: 'ambiguous_search');
        return _handle(MethodCall('playItem', {...args, 'id': (matches.single as Map)['id']}));
      case 'jump':
        if (args.containsKey('scope') && args['scope'] != _itemScope) return false;
        if (args['id'] case final String target) {
          if (args['revision'] != _catalogRevision || _spoken == null) return false;
          return _handle(MethodCall('playItem', {...args, 'id': target}));
        }
        final index = args['index'] as int?;
        if (args['revision'] != _catalogRevision || index == null || index < 0 || index >= _music.queue.length) {
          throw PlatformException(code: 'stale_queue');
        }
        await _music.jumpTo(index);
        return true;
      case 'play':
        if (!await commit()) return false;
        return _carTransport('play');
      case 'pause':
        if (!await commit()) return false;
        return _carTransport('pause');
      case 'next':
        if (!await commit()) return false;
        return _carTransport('next');
      case 'previous':
        if (!await commit()) return false;
        return _carTransport('previous');
      case 'connection':
        final connected = args['connected'] == true;
        _videoCapable = connected && args['video'] == true && Platform.isIOS;
        if (!connected) {
          _playlistGeneration++;
          ++_videoIntent;
          await _stopCarVideo();
          if (!mounted || !requestCurrent()) return false;
        }

        if (_connected && !connected) {
          context.read<GuestJukebox?>()?.close();
          await _carTransport('pause');
          if (!_current) return false;
        }
        if (!connected && !_bluetoothConnected) {
          final route = _automaticDrivingRoute;
          if (route != null && route.navigator != null) route.navigator!.removeRoute(route);
        }
        _connected = connected;
        MusicBackgroundFrames.carConnected = connected;
        if (connected) _openDrivingIfRequested();
        return true;
      default:
        throw MissingPluginException();
    }
  }

  bool _enqueueRequested(List<MediaItem> tracks, Map<String, dynamic> args) {
    if (args['queue'] == 'next') {
      _music.addNext(tracks);
      return true;
    }
    if (args['queue'] == 'later') {
      _music.addToEnd(tracks);
      return true;
    }
    return false;
  }

  static const _pageSize = 99;

  Map<String, Object?> _nextPage(String id, int offset) =>
      _folder('page:$offset:${base64Url.encode(utf8.encode(id))}', t.fileSources.loadMore);

  List<Map<String, Object?>> _page(Iterable<Map<String, Object?>> rows, String id, int offset) {
    final window = rows.skip(offset).take(_pageSize + 1).toList();
    return [...window.take(_pageSize), if (window.length > _pageSize) _nextPage(id, offset + _pageSize)];
  }

  Iterable<Map<String, Object?>> _containerActions(String id) => [
    {'id': id, 'title': t.common.play, 'playable': true},
    {'id': 'shuffle:$id', 'title': t.common.shuffle, 'playable': true},
  ];

  List<Map<String, Object?>> _containerPage(String id, int offset, Iterable<Map<String, Object?>> children) {
    const size = _pageSize - 2;
    final page = children.skip(offset).take(size + 1).toList();
    return [
      if (offset == 0) ..._containerActions(id),
      ...page.take(size),
      if (page.length > size) _nextPage(id, offset + size),
    ];
  }

  Iterable<Map<String, Object?>> _offlineGroups(List<MediaItem> tracks, {required bool albums}) {
    final provider = context.read<DownloadProvider>();
    final groups = <String, MediaItem>{};
    final counts = <String, Set<String>>{};
    for (final track in tracks) {
      final key = albums ? track.parentId : track.grandparentId;
      final title = albums ? track.albumTitle : track.albumArtistTitle ?? track.trackArtistTitle;
      if (key == null || key.isEmpty || title == null) continue;
      final group = track.copyWith(id: key, title: title, kind: albums ? MediaKind.album : MediaKind.artist);
      groups[group.globalKey] = group;
      (counts[group.globalKey] ??= {}).add(track.globalKey);
    }
    final ordered = groups.values.toList()..sort((a, b) => a.displayTitle.compareTo(b.displayTitle));
    return ordered.map((item) {
      final total = albums ? provider.getMetadata(item.globalKey)?.leafCount : null;
      final count = counts[item.globalKey]!.length;
      return {
        ..._item(item, offline: true),
        'subtitle': total != null && total >= count
            ? '$count/$total · ${t.navigation.downloads}'
            : '$count · ${t.libraries.groupings.tracks}',
      };
    });
  }

  Set<String> _hiddenCarLibraries() => {
    ...?context.read<HiddenLibrariesProvider?>()?.hiddenLibraryKeys,
    for (final library in context.read<LibrariesProvider?>()?.libraries ?? [])
      if (library.hidden) library.globalKey,
  };

  void _drivingOwnerChanged() {
    _routeMonitor?.refresh();
    if (!_current || _profiles.isBinding || !_profiles.lastBindingSucceeded) {
      ++_videoIntent;
      unawaited(_stopCarVideo());
      final automatic = _automaticDrivingRoute;
      if (automatic != null) {
        // Profile notifications may arrive during a provider rebuild. Retire
        // only this host's automatic route after that frame has settled.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          automatic.navigator?.removeRoute(automatic);
        });
        WidgetsBinding.instance.ensureVisualUpdate();
      }
    }
  }

  void _bluetoothRouteChanged() {
    final monitor = _routeMonitor, routes = _drivingRoutes;
    if (!_current || monitor == null || routes == null) return;
    if (!monitor.foreground) {
      _bluetoothConnected = false;
      return;
    }
    final selected = monitor.value;
    if (selected == null) return; // Unknown is not a disconnect event.
    final wasConnected = _bluetoothConnected;
    final platform = defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android';
    _bluetoothConnected = routes.matches(platform, selected);
    if (_bluetoothConnected && !wasConnected) _openDrivingIfRequested();
    if (wasConnected && !_bluetoothConnected && !_connected) {
      final route = _automaticDrivingRoute;
      if (route != null) route.navigator?.removeRoute(route);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      if (_guestPending) {
        _openGuestWhenAvailable();
      } else if (_connected) {
        _openDrivingIfRequested();
      }
    }
  }

  bool _guestPending = false;
  bool _guestOpen = false;
  void _openGuestWhenAvailable() {
    if (!_guestPending ||
        _guestOpen ||
        !_current ||
        _profiles.isBinding ||
        !_profiles.lastBindingSucceeded ||
        WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
      return;
    }
    if (PlatformDetector.isAutomotiveMedia() &&
        CarUxRestrictionsService.instance.state != CarUxRestrictionState.unrestricted) {
      return;
    }
    final screen = widget.navigatorKey.currentState?.overlay?.context;
    if (screen == null) return;
    _guestPending = false;
    _guestOpen = true;
    unawaited(showGuestJukebox(screen).whenComplete(() => _guestOpen = false));
  }

  void _openDrivingIfRequested() {
    if (!mounted ||
        !_current ||
        (!_connected && !_bluetoothConnected) ||
        _profiles.isBinding ||
        !_profiles.lastBindingSucceeded ||
        _drivingOpen ||
        WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed ||
        !(SettingsService.instanceOrNull?.read(SettingsService.musicAutoDriving) ?? false)) {
      return;
    }
    final navigator = widget.navigatorKey.currentState;
    if (navigator == null) return;
    _drivingOpen = true;
    final route = MaterialPageRoute<void>(builder: (_) => const MusicDrivingScreen());
    _automaticDrivingRoute = route;
    unawaited(
      navigator.push<void>(route).whenComplete(() {
        _drivingOpen = false;
        _automaticDrivingRoute = null;
      }),
    );
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
