import 'dart:async';

import 'package:flutter/foundation.dart' show listEquals;

import '../connection/connection.dart';
import '../connection/connection_registry.dart';
import '../profiles/active_profile_provider.dart';
import '../profiles/profile_plex_settings_account.dart';
import '../profiles/profile_connection_registry.dart';
import '../utils/app_logger.dart';
import 'home_layout_sync_service.dart';

/// Hands [HomeLayoutSyncService] the active profile's own Plex token whenever
/// the profile settles (startup, a switch, a freshly minted Home-user token),
/// and null for a profile without one (Jellyfin-only, not yet minted).
///
/// Home profiles never fall back to the owner's token: the layout is stored
/// on the account that token belongs to, and the owner's copy is not this
/// profile's layout.
class HomeLayoutSyncBinder {
  HomeLayoutSyncBinder({
    required ActiveProfileProvider activeProfile,
    required ConnectionRegistry connections,
    required ProfileConnectionRegistry profileConnections,
  }) : this._(activeProfile, connections, profileConnections);

  HomeLayoutSyncBinder._(this._activeProfile, this._connections, this._profileConnections) {
    _activeProfile.addListener(_onProfileChanged);
    _accounts = _connections
        .watchConnections()
        .map(
          (rows) => [for (final account in rows.whereType<PlexAccountConnection>()) (account.id, account.accountToken)],
        )
        .distinct(listEquals)
        .listen((_) => _onCredentialsChanged(), onError: _onCredentialError);
    _links = _profileConnections
        .watchAll()
        .map(
          (rows) => [
            for (final link in rows.where((row) => row.profileId == _activeProfile.activeId))
              (link.connectionId, link.userIdentifier, link.userToken, link.isDefault),
          ],
        )
        .distinct(listEquals)
        .listen((_) => _onCredentialsChanged(), onError: _onCredentialError);
    _onProfileChanged();
  }

  final ActiveProfileProvider _activeProfile;
  final ConnectionRegistry _connections;
  final ProfileConnectionRegistry _profileConnections;
  late final StreamSubscription<Object?> _accounts, _links;
  String? _lastProfileId;
  int _generation = 0;
  bool _disposed = false;

  void _onCredentialsChanged() {
    if (_disposed) return;
    HomeLayoutSyncService.instance.bind(null);
    _onProfileChanged();
  }

  void _onCredentialError(Object error, StackTrace stack) {
    if (_disposed) return;
    _generation++;
    HomeLayoutSyncService.instance.bind(null);
    appLogger.w('HomeLayoutSync: account subscription failed', error: error, stackTrace: stack);
  }

  void _onProfileChanged() {
    if (_disposed) return;
    final generation = ++_generation;
    final profile = _activeProfile.active;
    if (_lastProfileId != profile?.id) HomeLayoutSyncService.instance.bind(null);
    _lastProfileId = profile?.id;
    if (_activeProfile.isBinding || !_activeProfile.lastBindingSucceeded || profile == null) {
      HomeLayoutSyncService.instance.bind(null);
      return;
    }
    unawaited(() async {
      try {
        final resolved = await resolveProfilePlexSettingsAccount(
          profile: profile,
          connections: _connections,
          profileConnections: _profileConnections,
        );
        if (_disposed || generation != _generation) return;
        HomeLayoutSyncService.instance.bind(
          resolved?.token,
          accountId: resolved?.identity.account.id,
          userUuid: resolved?.identity.userUuid,
          localProfileId: profile.id,
        );
      } catch (e, s) {
        if (_disposed || generation != _generation) return;
        HomeLayoutSyncService.instance.bind(null);
        appLogger.w('HomeLayoutSync: token resolve failed', error: e, stackTrace: s);
      }
    }());
  }

  void dispose() {
    _disposed = true;
    HomeLayoutSyncService.instance.bind(null);
    _activeProfile.removeListener(_onProfileChanged);
    unawaited(_accounts.cancel());
    unawaited(_links.cancel());
  }
}
