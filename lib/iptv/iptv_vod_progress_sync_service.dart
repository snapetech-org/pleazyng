import 'dart:convert';

import 'package:flutter/widgets.dart';
import 'package:provider/provider.dart';

import '../connection/connection_registry.dart';
import '../profiles/active_profile_provider.dart';
import '../profiles/profile.dart';
import '../profiles/profile_connection_registry.dart';
import '../profiles/profile_plex_settings_account.dart';
import '../services/plex_auth_service.dart';
import '../services/settings_service.dart';
import '../utils/app_logger.dart';
import '../utils/serial_future_queue.dart';
import 'iptv_source_repository.dart';
import 'iptv_synced_vod_progress.dart';

/// Optional Plex sync of hashed IPTV source identities and playback positions.
class IptvVodProgressSyncService {
  IptvVodProgressSyncService._();

  @visibleForTesting
  IptvVodProgressSyncService.forTesting(this._auth);

  static final instance = IptvVodProgressSyncService._();
  final _operations = SerialFutureQueue();
  PlexAuthService? _auth;

  Future<void> syncActiveProfile(
    BuildContext context, {
    required String profileId,
    required IptvSourceRepository repository,
  }) async {
    if (!SettingsService.instance.read(SettingsService.iptvVodProgressSyncEnabled)) return;
    final active = context.read<ActiveProfileProvider>();
    final profile = active.active;
    if (profile == null || profile.id != profileId) return;
    final generation = active.identityMutationGeneration;
    final connections = context.read<ConnectionRegistry>();
    final links = context.read<ProfileConnectionRegistry>();
    await sync(
      profile: profile,
      repository: repository,
      resolveToken: () async => (await resolveProfilePlexSettingsAccount(
        profile: profile,
        connections: connections,
        profileConnections: links,
      ))?.token,
      isCurrent: () =>
          context.mounted &&
          active.activeId == profileId &&
          active.identityMutationGeneration == generation &&
          !active.isBinding &&
          active.lastBindingSucceeded,
    );
  }

  @visibleForTesting
  Future<void> sync({
    required Profile profile,
    required Future<String?> Function() resolveToken,
    required IptvSourceRepository repository,
    required bool Function() isCurrent,
  }) => _operations.run(() async {
    bool current() => isCurrent() && SettingsService.instance.read(SettingsService.iptvVodProgressSyncEnabled);
    try {
      if (!current()) return;
      final token = await resolveToken();
      if (token == null || !current()) return;
      final id = vodProgressSettingId(profile);
      final auth = _auth ??= await PlexAuthService.create();
      if (!current()) return;
      final entries = await auth.getUserSettings(token);
      if (!current()) return;
      final entry = entries.singleWhere((e) => e['id'] == id, orElse: () => const {});
      final remote = entry.isEmpty ? <SyncedVodProgress>[] : SyncedVodProgress.decode(entry['value']);
      if (remote.isNotEmpty) await repository.mergeVodProgressFromSync(profile.id, remote, isCurrent: current);
      if (!current()) return;
      final local = await repository.exportVodProgressForSync(profile.id);
      if (!current()) return;
      final merged = SyncedVodProgress.merge(remote, local.map(SyncedVodProgress.fromJson));
      if (jsonEncode(merged) == jsonEncode(remote)) return;
      await auth.putUserSetting(token, id, jsonEncode({'v': 2, 'rows': merged}));
    } on Object catch (error, stackTrace) {
      appLogger.w('IPTV VOD progress sync failed', error: error, stackTrace: stackTrace);
    }
  });
}
