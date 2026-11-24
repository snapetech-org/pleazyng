import 'dart:io';
import 'dart:isolate';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:drift/drift.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

import '../database/app_database.dart';
import '../media/live_tv_support.dart';
import '../models/livetv_channel.dart';
import '../models/livetv_program.dart';
import '../services/credential_vault.dart';
import '../sources/source_models.dart';
import '../sources/source_transport.dart';
import '../sources/universal_media_input.dart';
import 'iptv_guide_merge.dart';
import 'iptv_synced_vod_progress.dart';
import 'iptv_backup_match.dart';
import 'iptv_lineup_export.dart';
import 'iptv_series_rules.dart';
import 'iptv_live_tv_session.dart';
import 'dispatcharr_dvr_client.dart';
import 'stalker_portal_client.dart';
import 'iptv_models.dart';
import 'iptv_xtream_live_catalog.dart';
import 'iptv_hls_archive.dart';
import 'iptv_playlist_parser.dart';

part 'source_repository/stored_source.dart';
part 'source_repository/lineup.dart';
part 'source_repository/vod_catalog.dart';

class IptvImportException implements Exception {
  const IptvImportException(this.code);
  final String code;

  @override
  String toString() => 'IptvImportException($code)';
}

/// Profile-scoped IPTV catalog and import service. Provider URLs, passwords
/// and resolved stream addresses are encrypted before they enter SQLite.
class IptvSourceRepository {
  IptvSourceRepository(this._database);

  final AppDatabase _database;
  late final seriesRules = IptvSeriesRules(_database, (profile, source) => channels(profile, sourceId: source));
  static const _uuid = Uuid();
  static const maxPlaylistBytes = IptvPlaylistParser.maxCharacters;
  static const maxUploadedBytes = IptvPlaylistParser.maxUploadedBytes;
  static const maxXmltvBytes = IptvXmltvParser.maxCharacters;

  Future<String> createSource({
    required String profileId,
    required String displayName,
    required IptvInputKind kind,
    required String endpoint,
    String? playlistContents,
    String guideEndpoint = '',
    List<String> additionalGuideEndpoints = const [],
    List<String> backupSourceIds = const [],
    String username = '',
    String password = '',
    String macAddress = '',
    String dispatcharrApiEndpoint = '',
    String dispatcharrApiKey = '',
    bool localTimeShiftEnabled = true,
    bool allowHttp = false,
  }) async {
    final config = IptvSourceConfiguration(
      id: _uuid.v4(),
      profileId: profileId,
      displayName: displayName.trim(),
      kind: kind,
      endpoint: endpoint.trim(),
      guideEndpoint: guideEndpoint.trim(),
      backupSourceIds: backupSourceIds.toSet().toList(),
      additionalGuideEndpoints: additionalGuideEndpoints
          .map((value) => value.trim())
          .where((value) => value.isNotEmpty)
          .toList(),
      username: username,
      password: password,
      macAddress: macAddress.trim().toUpperCase(),
      dispatcharrApiEndpoint: dispatcharrApiEndpoint.trim(),
      dispatcharrApiKey: dispatcharrApiKey.trim(),
      localTimeShiftEnabled: localTimeShiftEnabled,
      allowHttp: allowHttp,
    );
    _validate(config);
    final protectedPlaylist = await _protectPlaylist(config, playlistContents);
    if (kind == IptvInputKind.m3uFile && protectedPlaylist == null) throw const IptvImportException('source_invalid');
    final protectedConfig = await CredentialVault.protect(jsonEncode(config.toJson()));
    await _database.runIdentityMutation(
      () => _database.customStatement(
        'INSERT INTO iptv_sources(id, profile_id, display_name, kind, config_ciphertext, playlist_ciphertext, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
          config.id,
          config.profileId,
          config.displayName,
          config.kind.name,
          protectedConfig,
          protectedPlaylist,
          DateTime.now().millisecondsSinceEpoch,
        ],
      ),
    );
    return config.id;
  }

  Future<String?> _protectPlaylist(IptvSourceConfiguration config, String? contents) async {
    if (contents == null) return null;
    if (config.kind != IptvInputKind.m3uFile || contents.length > maxUploadedBytes) {
      throw const IptvImportException('source_invalid');
    }
    late final IptvPlaylistParseResult parsed;
    try {
      parsed = await IptvPlaylistParser.load(
        contents,
        base: config.playlistUri(),
        allowHttp: config.allowHttp,
        byteLimit: maxUploadedBytes,
      );
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    }
    if (parsed.isHls) throw const IptvImportException('playlist_is_stream');
    if (parsed.entries.isEmpty) throw const IptvImportException('invalid_data');
    return CredentialVault.protect(contents);
  }

  Future<void> updateSource(IptvSourceConfiguration config, {String? playlistContents}) async {
    _validate(config);
    final isFile = config.kind == IptvInputKind.m3uFile;
    final protectedPlaylist = await _protectPlaylist(config, playlistContents);
    final protectedConfig = await CredentialVault.protect(jsonEncode(config.toJson()));
    final changed = await _database.runIdentityMutation(
      () => _database.customUpdate(
        '''UPDATE iptv_sources SET display_name=?, kind=?, config_ciphertext=?,
        playlist_ciphertext=CASE WHEN ? THEN COALESCE(?, playlist_ciphertext) END,
        revision=revision+1, last_error_code=NULL WHERE id=? AND profile_id=?
        AND (?=0 OR ?=1 OR playlist_ciphertext IS NOT NULL)''',
        variables: [
          Variable(config.displayName.trim()),
          Variable(config.kind.name),
          Variable(protectedConfig),
          Variable(isFile ? 1 : 0),
          Variable(protectedPlaylist),
          Variable(config.id),
          Variable(config.profileId),
          Variable(isFile ? 1 : 0),
          Variable(protectedPlaylist != null ? 1 : 0),
        ],
      ),
    );
    if (changed != 1) {
      throw const IptvImportException('source_missing');
    }
  }

  Future<IptvSourceConfiguration?> configuration(String profileId, String sourceId) async {
    final row = await _database
        .customSelect(
          'SELECT config_ciphertext FROM iptv_sources WHERE profile_id=? AND id=?',
          variables: [Variable(profileId), Variable(sourceId)],
        )
        .getSingleOrNull();
    if (row == null) {
      return null;
    }
    final clear = await CredentialVault.reveal(row.read<String>('config_ciphertext'));
    if (clear == null) {
      throw const IptvImportException('credential_unavailable');
    }
    try {
      return IptvSourceConfiguration.fromJson(jsonDecode(clear) as Map<String, dynamic>);
    } on Object {
      throw const IptvImportException('source_invalid');
    }
  }

  Future<List<IptvSourceSummary>> sources(String profileId) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT s.id, s.profile_id, s.display_name, s.kind, s.generation, s.last_success_at, s.last_error_code,
             (SELECT COUNT(*) FROM iptv_channels c WHERE c.source_id=s.id) AS channel_count,
             (SELECT COUNT(*) FROM iptv_programmes p WHERE p.source_id=s.id) AS programme_count
      FROM iptv_sources s WHERE s.profile_id=? ORDER BY s.created_at
    ''',
          variables: [Variable(profileId)],
        )
        .get();
    return [
      for (final row in rows)
        IptvSourceSummary(
          id: row.read<String>('id'),
          profileId: row.read<String>('profile_id'),
          displayName: row.read<String>('display_name'),
          kind: _kind(row.read<String>('kind')),
          generation: row.read<int>('generation'),
          channelCount: row.read<int>('channel_count'),
          programmeCount: row.read<int>('programme_count'),
          lastSuccessAt: row.readNullable<int>('last_success_at') == null
              ? null
              : DateTime.fromMillisecondsSinceEpoch(row.read<int>('last_success_at')),
          lastErrorCode: row.readNullable<String>('last_error_code'),
        ),
    ];
  }

  Future<List<LiveTvChannel>> channels(String profileId, {String? sourceId}) async {
    final sourceRows = await _database
        .customSelect(
          'SELECT id, display_name FROM iptv_sources WHERE profile_id=? AND (? IS NULL OR id=?) ORDER BY created_at',
          variables: [Variable(profileId), Variable(sourceId), Variable(sourceId)],
        )
        .get();
    final channels = <LiveTvChannel>[];
    for (final source in sourceRows) {
      final sourceId = source.read<String>('id');
      final rows = await _database
          .customSelect(
            'SELECT channel_id, title, tvg_id, number, group_title, logo_url, is_radio, catchup_days, catchup_source_ciphertext FROM iptv_channels WHERE source_id=?',
            variables: [Variable(sourceId)],
          )
          .get();
      final overrideRows = await _database
          .customSelect(
            'SELECT channel_id, favorite, favorite_order, lineup_order, hidden, custom_title, group_override, guide_id_override, parental_locked FROM iptv_channel_overrides WHERE profile_id=? AND source_id=?',
            variables: [Variable(profileId), Variable(sourceId)],
          )
          .get();
      final groupLockRows = await _database
          .customSelect(
            'SELECT group_key FROM iptv_group_locks WHERE profile_id=? AND source_id=?',
            variables: [Variable(profileId), Variable(sourceId)],
          )
          .get();
      final lockedGroups = {for (final row in groupLockRows) row.read<String>('group_key')};
      final overrides = {for (final row in overrideRows) row.read<String>('channel_id'): row};
      final snapshots = <IptvChannelSnapshot>[];
      for (final row in rows) {
        final id = row.read<String>('channel_id');
        final override = overrides[id];
        if (override?.read<int>('hidden') == 1) {
          continue;
        }
        final customTitle = override?.readNullable<String>('custom_title')?.trim();
        final groupOverride = override?.readNullable<String>('group_override')?.trim();
        final guideIdOverride = override?.readNullable<String>('guide_id_override')?.trim();
        final groupTitle = groupOverride != null && groupOverride.isNotEmpty
            ? groupOverride
            : row.readNullable<String>('group_title');
        snapshots.add(
          IptvChannelSnapshot(
            id: id,
            sourceId: sourceId,
            title: customTitle != null && customTitle.isNotEmpty ? customTitle : row.read<String>('title'),
            tvgId: guideIdOverride != null && guideIdOverride.isNotEmpty
                ? guideIdOverride
                : row.readNullable<String>('tvg_id'),
            number: row.readNullable<String>('number'),
            groupTitle: groupTitle,
            logoUrl: row.readNullable<String>('logo_url'),
            radio: row.read<int>('is_radio') == 1,
            hasCatchup: row.readNullable<String>('catchup_source_ciphertext') != null,
            catchupDays: row.readNullable<int>('catchup_days'),
            lineupOrder: override?.readNullable<int>('lineup_order'),
            favorite: override?.read<int>('favorite') == 1,
            favoriteOrder: override?.read<int>('favorite_order') ?? 0,
            parentalLocked:
                override?.read<int>('parental_locked') == 1 || lockedGroups.contains(iptvGroupLockKey(groupTitle)),
          ),
        );
      }
      snapshots.sort((a, b) {
        final rank = (a.lineupOrder ?? 0x7fffffff).compareTo(b.lineupOrder ?? 0x7fffffff);
        if (rank != 0) return rank;
        final number = (double.tryParse(a.number ?? '') ?? double.infinity).compareTo(
          double.tryParse(b.number ?? '') ?? double.infinity,
        );
        return number != 0 ? number : a.title.toLowerCase().compareTo(b.title.toLowerCase());
      });
      channels.addAll(snapshots.map((channel) => channel.toLiveTvChannel(source.read<String>('display_name'))));
    }
    return channels;
  }

  /// Take one consistent snapshot, including profile visibility and group locks.
  Future<String> exportLineup(
    String profileId,
    String sourceId,
    Set<String> selected, {
    required bool guide,
  }) => _database.transaction(() async {
    final source = await _source(profileId, sourceId);
    if (source == null) throw const IptvImportException('source_missing');
    final visible = (await channels(
      profileId,
      sourceId: sourceId,
    )).where((channel) => !channel.iptvParentalLocked && selected.contains(channel.iptvChannelId)).toList();
    if (guide) {
      final programmes = await schedule(profileId, DateTime.utc(1970), DateTime.utc(9999), sourceId: sourceId);
      return xmltvLineup(visible, programmes);
    }
    // A Stalker portal issues a new play link on demand for each tuning session.
    if (source.kind == IptvInputKind.stalker.name) throw const IptvImportException('dynamic_stream');
    final rows = await _database
        .customSelect(
          'SELECT channel_id,stream_url_ciphertext,stream_headers_ciphertext,catchup_source_ciphertext FROM iptv_channels WHERE source_id=?',
          variables: [Variable(sourceId)],
        )
        .get();
    final byId = {for (final row in rows) row.read<String>('channel_id'): row};
    Future<String?> decrypt(String? ciphertext) async {
      if (ciphertext == null) return null;
      final value = await CredentialVault.reveal(ciphertext);
      if (value == null) throw const IptvImportException('credential_unavailable');
      return value;
    }

    final output = StringBuffer('#EXTM3U\n');
    for (final channel in visible) {
      final row = byId[channel.iptvChannelId]!;
      final url = await decrypt(row.read<String>('stream_url_ciphertext'));
      final headers = await decrypt(row.readNullable<String>('stream_headers_ciphertext'));
      final catchup = await decrypt(row.readNullable<String>('catchup_source_ciphertext'));
      output.write(m3uStation(channel, url!, headers: headers, catchup: catchup));
    }
    return output.toString();
  });

  Future<List<IptvEditableChannel>> editableChannels(String profileId, String sourceId) async {
    final source = await _source(profileId, sourceId);
    if (source == null) {
      throw const IptvImportException('source_missing');
    }
    final rows = await _database
        .customSelect(
          '''
      SELECT c.channel_id, c.title, c.tvg_id, c.number, c.group_title, c.logo_url,
             COALESCE(o.hidden, 0) AS hidden, o.custom_title, o.group_override, o.guide_id_override,
             COALESCE(o.parental_locked, 0) AS parental_locked
      FROM iptv_channels c LEFT JOIN iptv_channel_overrides o
        ON o.profile_id=? AND o.source_id=c.source_id AND o.channel_id=c.channel_id
      WHERE c.source_id=? ORDER BY o.lineup_order IS NULL, o.lineup_order, c.number IS NULL, CAST(c.number AS REAL), c.title COLLATE NOCASE, c.channel_id
    ''',
          variables: [Variable(profileId), Variable(sourceId)],
        )
        .get();
    return [
      for (final row in rows)
        IptvEditableChannel(
          id: row.read<String>('channel_id'),
          title: row.read<String>('title'),
          number: row.readNullable<String>('number'),
          groupTitle: row.readNullable<String>('group_title'),
          tvgId: row.readNullable<String>('tvg_id'),
          guideIdOverride: row.readNullable<String>('guide_id_override'),
          logoUrl: row.readNullable<String>('logo_url'),
          hidden: row.read<int>('hidden') == 1,
          customTitle: row.readNullable<String>('custom_title'),
          groupOverride: row.readNullable<String>('group_override'),
          parentalLocked: row.read<int>('parental_locked') == 1,
        ),
    ];
  }

  Future<List<IptvLineupGroup>> lineupGroups(String profileId, String sourceId) async {
    final rows = await _database
        .customSelect(
          'SELECT g.id,g.name,g.members,g.revision FROM iptv_lineup_groups g JOIN iptv_sources s ON s.id=g.source_id WHERE s.profile_id=? AND s.id=? ORDER BY g.name COLLATE NOCASE,g.id',
          variables: [Variable(profileId), Variable(sourceId)],
        )
        .get();
    return [
      for (final row in rows)
        (
          id: row.read<String>('id'),
          name: row.read<String>('name'),
          members: (jsonDecode(row.read<String>('members')) as List).cast<String>().toSet(),
          revision: row.read<int>('revision'),
        ),
    ];
  }

  /// An absent feed keeps its memberships across outages. New memberships must
  /// identify a channel in this source's current catalog.
  Future<void> editLineupGroup(
    String profileId,
    String sourceId, {
    IptvLineupGroup? original,
    required String name,
    Set<String> add = const {},
    Set<String> remove = const {},
    bool delete = false,
  }) async {
    final title = name.trim();
    if (title.isEmpty || title.length > 120) throw const IptvImportException('source_invalid');
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        if (await _source(profileId, sourceId) == null) throw const IptvImportException('source_missing');
        final groups = await lineupGroups(profileId, sourceId);
        final current = groups.where((g) => g.id == original?.id).firstOrNull;
        if (original != null && original.revision != current?.revision) {
          throw const IptvImportException('source_changed');
        }
        if (delete && current != null) {
          await _database.customStatement('DELETE FROM iptv_lineup_groups WHERE source_id=? AND id=?', [
            sourceId,
            current.id,
          ]);
          return;
        }
        if (groups.any((g) => g.id != current?.id && g.name.toLowerCase() == title.toLowerCase())) {
          throw const IptvImportException('source_changed');
        }
        if (add.isNotEmpty) {
          final catalog = await _database
              .customSelect('SELECT channel_id FROM iptv_channels WHERE source_id=?', variables: [Variable(sourceId)])
              .get();
          if (!catalog.map((row) => row.read<String>('channel_id')).toSet().containsAll(add)) {
            throw const IptvImportException('source_changed');
          }
        }
        final members = {...?current?.members, ...add}..removeAll(remove);
        final encoded = jsonEncode(members.toList()..sort());
        if (current == null) {
          await _database.customStatement(
            'INSERT INTO iptv_lineup_groups(id,source_id,name,members) VALUES (?,?,?,?)',
            [_uuid.v4(), sourceId, title, encoded],
          );
        } else {
          await _database.customStatement(
            'UPDATE iptv_lineup_groups SET name=?,members=?,revision=revision+1 WHERE source_id=? AND id=?',
            [title, encoded, sourceId, current.id],
          );
        }
      }),
    );
  }

  /// Persist only visibility so concurrent edits to station names, locks and favorites survive.
  Future<void> setChannelsHidden(
    String profileId,
    String sourceId,
    Iterable<String> channelIds,
    bool hidden, {
    bool? expectedHidden,
  }) async {
    final ids = channelIds.toSet().toList();
    if (ids.isEmpty) return;
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        if (await _source(profileId, sourceId) == null) throw const IptvImportException('source_missing');
        for (var offset = 0; offset < ids.length; offset += 400) {
          final group = ids.skip(offset).take(400).toList();
          final placeholders = List.filled(group.length, '?').join(',');
          if (expectedHidden != null) {
            final rows = await _database
                .customSelect(
                  'SELECT COUNT(*) AS matches FROM iptv_channels c LEFT JOIN iptv_channel_overrides o ON o.source_id=c.source_id AND o.channel_id=c.channel_id AND o.profile_id=? WHERE c.source_id=? AND COALESCE(o.hidden,0)=? AND c.channel_id IN ($placeholders)',
                  variables: [
                    Variable(profileId),
                    Variable(sourceId),
                    Variable(expectedHidden ? 1 : 0),
                    for (final id in group) Variable(id),
                  ],
                )
                .getSingle();
            if (rows.read<int>('matches') != group.length) throw const IptvImportException('source_changed');
          }
          await _database.customStatement(
            'INSERT INTO iptv_channel_overrides(profile_id,source_id,channel_id,hidden) '
            'SELECT ?,source_id,channel_id,? FROM iptv_channels WHERE source_id=? AND channel_id IN ($placeholders) '
            'ON CONFLICT(profile_id,source_id,channel_id) DO UPDATE SET hidden=excluded.hidden',
            [profileId, hidden ? 1 : 0, sourceId, ...group],
          );
        }
      }),
    );
  }

  /// Compare the full order inside the transaction to detect a refresh or another editor.
  Future<void> saveLineupOrder(String profileId, String sourceId, List<String> original, List<String> ordered) async {
    final ids = ordered.toSet();
    if (ids.length != original.length || ids.length != ordered.length || !ids.containsAll(original)) {
      throw const IptvImportException('source_changed');
    }
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        final current = await editableChannels(profileId, sourceId);
        if (current.length != original.length) throw const IptvImportException('source_changed');
        for (var i = 0; i < current.length; i++) {
          if (current[i].id != original[i]) throw const IptvImportException('source_changed');
        }
        await _database.batch((batch) {
          for (var i = 0; i < ordered.length; i++) {
            batch.customStatement(
              'INSERT INTO iptv_channel_overrides(profile_id,source_id,channel_id,lineup_order) VALUES (?,?,?,?) '
              'ON CONFLICT(profile_id,source_id,channel_id) DO UPDATE SET lineup_order=excluded.lineup_order',
              [profileId, sourceId, ordered[i], i],
            );
          }
        });
      }),
    );
  }

  Future<void> saveChannelPresentation({
    required String profileId,
    required String sourceId,
    required String channelId,
    required bool hidden,
    required String? customTitle,
    required String? groupOverride,
    required String? guideIdOverride,
    required bool parentalLocked,
  }) async {
    final source = await _source(profileId, sourceId);
    if (source == null) {
      throw const IptvImportException('source_missing');
    }
    final title = customTitle?.trim();
    final group = groupOverride?.trim();
    final guideId = guideIdOverride?.trim();
    if ((title?.length ?? 0) > 160 || (group?.length ?? 0) > 120 || (guideId?.length ?? 0) > 180) {
      throw const IptvImportException('source_invalid');
    }
    final changed = await _database.runIdentityMutation(
      () => _database.customUpdate(
        '''
      INSERT INTO iptv_channel_overrides(
        profile_id, source_id, channel_id, favorite, favorite_order, hidden, custom_title, group_override, guide_id_override, parental_locked
      )
      SELECT ?, ?, channel_id, 0, 0, ?, ?, ?, ?, ? FROM iptv_channels WHERE source_id=? AND channel_id=?
      ON CONFLICT(profile_id, source_id, channel_id) DO UPDATE SET
        hidden=excluded.hidden, custom_title=excluded.custom_title, group_override=excluded.group_override,
        guide_id_override=excluded.guide_id_override, parental_locked=excluded.parental_locked
    ''',
        variables: [
          Variable(profileId),
          Variable(sourceId),
          Variable(hidden ? 1 : 0),
          Variable(title == null || title.isEmpty ? null : title),
          Variable(group == null || group.isEmpty ? null : group),
          Variable(guideId == null || guideId.isEmpty ? null : guideId),
          Variable(parentalLocked ? 1 : 0),
          Variable(sourceId),
          Variable(channelId),
        ],
      ),
    );
    if (changed != 1) {
      throw const IptvImportException('source_missing');
    }
  }

  Future<Set<String>> lockedGroups(String profileId, String sourceId) async {
    final rows = await _database
        .customSelect(
          'SELECT group_key FROM iptv_group_locks WHERE profile_id=? AND source_id=?',
          variables: [Variable(profileId), Variable(sourceId)],
        )
        .get();
    return {for (final row in rows) row.read<String>('group_key')};
  }

  Future<void> saveGroupLock({
    required String profileId,
    required String sourceId,
    required String groupTitle,
    required bool locked,
  }) async {
    final source = await _source(profileId, sourceId);
    final title = groupTitle.trim();
    if (source == null) {
      throw const IptvImportException('source_missing');
    }
    if (title.isEmpty || title.length > 120) {
      throw const IptvImportException('source_invalid');
    }
    final key = iptvGroupLockKey(title);
    await _database.runIdentityMutation(() async {
      if (locked) {
        await _database.customStatement(
          '''
          INSERT INTO iptv_group_locks(profile_id, source_id, group_key, group_title)
          VALUES (?, ?, ?, ?)
          ON CONFLICT(profile_id, source_id, group_key) DO UPDATE SET group_title=excluded.group_title
        ''',
          [profileId, sourceId, key, title],
        );
      } else {
        await _database.customStatement(
          'DELETE FROM iptv_group_locks WHERE profile_id=? AND source_id=? AND group_key=?',
          [profileId, sourceId, key],
        );
      }
    });
  }

  Future<List<FavoriteChannel>> favorites(String profileId) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT o.source_id, o.channel_id, COALESCE(NULLIF(o.custom_title, ''), c.title) AS title, c.logo_url, c.number
      FROM iptv_channel_overrides o JOIN iptv_channels c
        ON c.source_id=o.source_id AND c.channel_id=o.channel_id
      JOIN iptv_sources s ON s.id=o.source_id AND s.profile_id=o.profile_id
      WHERE o.profile_id=? AND o.favorite=1 AND o.hidden=0 ORDER BY o.favorite_order, c.number, c.title
    ''',
          variables: [Variable(profileId)],
        )
        .get();
    return [
      for (final row in rows)
        FavoriteChannel(
          source: iptvFavoriteSource(row.read<String>('source_id')),
          id: '${row.read<String>('source_id')}:${row.read<String>('channel_id')}',
          title: row.read<String>('title'),
          thumb: row.readNullable<String>('logo_url'),
          vcn: row.readNullable<String>('number'),
        ),
    ];
  }

  Future<void> saveFavoriteOrder(String profileId, String sourceId, List<String> channelIds) async {
    final source = await _source(profileId, sourceId);
    if (source == null) {
      throw const IptvImportException('source_missing');
    }
    final orderedIds = channelIds.toSet().toList();
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        await _database.customStatement(
          'UPDATE iptv_channel_overrides SET favorite=0 WHERE profile_id=? AND source_id=? AND favorite=1 AND hidden=0',
          [profileId, sourceId],
        );
        for (var index = 0; index < orderedIds.length; index++) {
          await _database.customStatement(
            '''
          INSERT INTO iptv_channel_overrides(profile_id, source_id, channel_id, favorite, favorite_order)
          SELECT ?, ?, channel_id, 1, ? FROM iptv_channels WHERE source_id=? AND channel_id=?
          ON CONFLICT(profile_id, source_id, channel_id) DO UPDATE SET favorite=1, favorite_order=excluded.favorite_order
        ''',
            [profileId, sourceId, index, sourceId, orderedIds[index]],
          );
        }
      }),
    );
  }

  Future<List<LiveTvProgram>> schedule(
    String profileId,
    DateTime from,
    DateTime to, {
    String? sourceId,
    String? channelId,
    bool matchupsOnly = false,
  }) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT p.source_id, p.channel_id, p.begins_at, p.ends_at, p.title, p.summary, p.category, p.episode_id, p.thumb_url
      FROM iptv_programmes p JOIN iptv_sources s ON s.id=p.source_id
      WHERE s.profile_id=? AND p.begins_at < ? AND p.ends_at > ?
        AND (? IS NULL OR p.source_id=?) AND (? IS NULL OR p.channel_id=?)
      ${matchupsOnly ? "AND (p.title LIKE '% vs %' OR p.title LIKE '% vs. %' OR p.title LIKE '% v %' OR p.title LIKE '% v. %' OR p.title LIKE '% at %' OR p.title LIKE '% @ %' OR p.title LIKE '% contre %' OR p.title LIKE '% gegen %' OR p.title LIKE '% contra %')" : ''}
      ORDER BY p.begins_at
    ''',
          variables: [
            Variable(profileId),
            Variable(to.millisecondsSinceEpoch ~/ 1000),
            Variable(from.millisecondsSinceEpoch ~/ 1000),
            Variable(sourceId),
            Variable(sourceId),
            Variable(channelId),
            Variable(channelId),
          ],
        )
        .get();
    return [
      for (final row in rows)
        IptvProgrammeSnapshot(
          channelId: row.read<String>('channel_id'),
          beginsAt: row.read<int>('begins_at'),
          endsAt: row.read<int>('ends_at'),
          title: row.read<String>('title'),
          summary: row.readNullable<String>('summary'),
          category: row.readNullable<String>('category'),
          episodeId: row.readNullable<String>('episode_id'),
          thumb: row.readNullable<String>('thumb_url'),
        ).toLiveTvProgram(row.read<String>('source_id')),
    ];
  }

  Future<List<IptvSourceSummary>> vodSources(String profileId) async {
    final available = <IptvSourceSummary>[];
    for (final source in await sources(profileId)) {
      if (!source.kind.supportsXtreamVod) {
        continue;
      }
      final config = await configuration(profileId, source.id);
      if (config?.supportsXtreamCatalog == true) {
        available.add(source);
      }
    }
    return available;
  }

  Future<List<IptvVodCategory>> fetchVodCategories(String profileId, String sourceId, IptvVodKind kind) async {
    final config = await configuration(profileId, sourceId);
    if (config == null) {
      throw const IptvImportException('source_missing');
    }
    if (!config.supportsXtreamCatalog) {
      throw const IptvImportException('vod_unavailable');
    }
    final action = kind == IptvVodKind.movie ? 'get_vod_categories' : 'get_series_categories';
    final stored = await _source(profileId, sourceId);
    if (stored == null) {
      throw const IptvImportException('source_missing');
    }
    final transport = SourceTransport(allowHttp: config.allowHttp);
    try {
      final response = await _xtreamJson(transport, config.playerApiUri(action));
      final rows = _xtreamRows(response);
      final categories = <IptvVodCategory>[];
      for (final value in rows) {
        final row = sourceMap(value);
        final id = _valueString(row, const ['category_id', 'id']);
        final name = _valueString(row, const ['category_name', 'name']);
        if (id == null || name == null) {
          continue;
        }
        categories.add(IptvVodCategory(id: id, name: name, kind: kind));
      }
      await _replaceVodCategories(stored, kind, categories);
      return categories;
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } on IptvImportException {
      rethrow;
    } on Object {
      throw const IptvImportException('provider_unavailable');
    } finally {
      transport.close();
    }
  }

  Future<List<IptvVodItem>> fetchVodItems(
    String profileId,
    String sourceId,
    IptvVodKind kind, {
    String? categoryId,
  }) async {
    final config = await configuration(profileId, sourceId);
    if (config == null) {
      throw const IptvImportException('source_missing');
    }
    if (!config.supportsXtreamCatalog) {
      throw const IptvImportException('vod_unavailable');
    }
    final stored = await _source(profileId, sourceId);
    if (stored == null) {
      throw const IptvImportException('source_missing');
    }
    final action = kind == IptvVodKind.movie ? 'get_vod_streams' : 'get_series';
    final transport = SourceTransport(allowHttp: config.allowHttp);
    try {
      final response = await _xtreamJson(
        transport,
        config.playerApiUri(action, parameters: categoryId == null ? const {} : {'category_id': categoryId}),
      );
      final rows = _xtreamRows(response);
      if (rows.length > IptvPlaylistParser.maxEntries) {
        throw const IptvImportException('invalid_data');
      }
      final items = <IptvVodItem>[];
      for (final value in rows) {
        final row = sourceMap(value);
        final id = _valueString(row, kind == IptvVodKind.movie ? const ['stream_id', 'id'] : const ['series_id', 'id']);
        final title = _valueString(row, const ['name', 'title']);
        if (id == null || title == null) {
          continue;
        }
        final info = sourceMap(row['info']);
        final poster = _safeVodArtwork(
          _valueString(row, const ['stream_icon', 'cover', 'cover_big', 'poster']) ??
              _valueString(info, const ['movie_image', 'cover', 'cover_big']),
          config,
        );
        final backdropRaw =
            _firstStringValue(row, const ['backdrop_path', 'backdrop', 'backdrop_url']) ??
            _firstStringValue(info, const ['backdrop_path', 'backdrop', 'backdrop_url']);
        items.add(
          IptvVodItem(
            id: id,
            sourceId: sourceId,
            kind: kind,
            title: _safeCatalogText(title, 240) ?? id,
            categoryId: _valueString(row, const ['category_id']),
            plot: _safeCatalogText(
              _valueString(row, const ['plot', 'description']) ?? _valueString(info, const ['plot']),
              4000,
            ),
            year: _valueInt(row, const ['year', 'releasedate']) ?? _valueInt(info, const ['releasedate', 'year']),
            durationSeconds:
                _valueInt(row, const ['duration_secs', 'duration']) ??
                _durationSeconds(_valueString(row, const ['duration']) ?? _valueString(info, const ['duration'])),
            rating: _valueDouble(row, const ['rating', 'rating_5based']),
            posterUrl: poster,
            backdropUrl: _safeVodArtwork(backdropRaw, config),
            containerExtension: _iptvSafeExtension(
              _valueString(row, const ['container_extension']) ??
                  _valueString(info, const ['container_extension']) ??
                  'mp4',
            ),
          ),
        );
      }
      await _replaceVodItems(stored, kind, categoryId, items);
      return items;
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } on IptvImportException {
      rethrow;
    } on Object {
      throw const IptvImportException('provider_unavailable');
    } finally {
      transport.close();
    }
  }

  Future<IptvVodSeriesDetails> fetchVodSeriesDetails(String profileId, String sourceId, IptvVodItem series) async {
    if (series.kind != IptvVodKind.series || series.sourceId != sourceId) {
      throw const IptvImportException('invalid_data');
    }
    final config = await configuration(profileId, sourceId);
    if (config == null || !config.supportsXtreamCatalog) {
      throw const IptvImportException('source_missing');
    }
    final transport = SourceTransport(allowHttp: config.allowHttp);
    try {
      final response = await _xtreamJson(
        transport,
        config.playerApiUri('get_series_info', parameters: {'series_id': series.id}),
      );
      final envelope = sourceMap(response);
      final info = sourceMap(envelope['info']);
      final episodeMap = envelope['episodes'];
      final episodes = <IptvVodEpisode>[];
      if (episodeMap is List) {
        for (final episode in episodeMap) {
          _appendEpisode(episodes, episode, config, null);
        }
      } else if (episodeMap is Map) {
        for (final seasonEntry in episodeMap.entries) {
          final season = int.tryParse('${seasonEntry.key}') ?? 0;
          final rows = seasonEntry.value is List ? seasonEntry.value as List : const [];
          for (final episode in rows) {
            _appendEpisode(episodes, episode, config, season);
          }
        }
      }
      episodes.sort((a, b) {
        final season = a.season.compareTo(b.season);
        return season != 0 ? season : a.episode.compareTo(b.episode);
      });
      final updatedSeries = IptvVodItem(
        id: series.id,
        sourceId: sourceId,
        kind: IptvVodKind.series,
        title: _safeCatalogText(_valueString(info, const ['name', 'title']) ?? series.title, 240) ?? series.id,
        categoryId: series.categoryId,
        plot: _safeCatalogText(_valueString(info, const ['plot', 'description']) ?? series.plot, 4000),
        year: _valueInt(info, const ['year', 'releaseDate']) ?? series.year,
        rating: _valueDouble(info, const ['rating']) ?? series.rating,
        posterUrl: _safeVodArtwork(
          _valueString(info, const ['cover', 'cover_big', 'movie_image']) ?? series.posterUrl,
          config,
        ),
        backdropUrl:
            _safeVodArtwork(_firstStringValue(info, const ['backdrop_path', 'backdrop', 'backdrop_url']), config) ??
            series.backdropUrl,
      );
      return IptvVodSeriesDetails(series: updatedSeries, episodes: episodes);
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } on IptvImportException {
      rethrow;
    } on Object {
      throw const IptvImportException('provider_unavailable');
    } finally {
      transport.close();
    }
  }

  Future<List<IptvVodCategory>> cachedVodCategories(String profileId, String sourceId, IptvVodKind kind) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT category_id, name FROM iptv_vod_categories WHERE source_id=? AND kind=? ORDER BY name COLLATE NOCASE
    ''',
          variables: [Variable(sourceId), Variable(kind.name)],
        )
        .get();
    return [
      for (final row in rows)
        IptvVodCategory(id: row.read<String>('category_id'), name: row.read<String>('name'), kind: kind),
    ];
  }

  Future<List<IptvVodItem>> cachedVodItems(
    String profileId,
    String sourceId,
    IptvVodKind kind, {
    String? categoryId,
  }) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT item_id, category_id, title, plot, year, duration_seconds, rating, poster_url, backdrop_url, container_extension
      FROM iptv_vod_items
      WHERE source_id=? AND kind=? AND (? IS NULL OR category_id=?)
      ORDER BY title COLLATE NOCASE
    ''',
          variables: [Variable(sourceId), Variable(kind.name), Variable(categoryId), Variable(categoryId)],
        )
        .get();
    return [
      for (final row in rows)
        IptvVodItem(
          id: row.read<String>('item_id'),
          sourceId: sourceId,
          kind: kind,
          categoryId: row.readNullable<String>('category_id'),
          title: row.read<String>('title'),
          plot: row.readNullable<String>('plot'),
          year: row.readNullable<int>('year'),
          durationSeconds: row.readNullable<int>('duration_seconds'),
          rating: row.readNullable<double>('rating'),
          posterUrl: row.readNullable<String>('poster_url'),
          backdropUrl: row.readNullable<String>('backdrop_url'),
          containerExtension: row.read<String>('container_extension'),
        ),
    ];
  }

  /// Searches the locally cached Xtream catalog without issuing provider
  /// requests for every global-search keystroke.
  Future<List<IptvVodItem>> searchVod(String profileId, String query, {int limit = 40}) async {
    final value = query.trim();
    if (value.isEmpty) {
      return const [];
    }
    final rows = await _database
        .customSelect(
          '''
      SELECT v.source_id, v.kind, v.item_id, v.category_id, v.title, v.plot, v.year,
             v.duration_seconds, v.rating, v.poster_url, v.backdrop_url, v.container_extension
      FROM iptv_vod_items v JOIN iptv_sources s ON s.id=v.source_id
      WHERE s.profile_id=? AND v.kind IN ('movie', 'series')
        AND (instr(lower(v.title), lower(?)) > 0 OR instr(lower(COALESCE(v.plot, '')), lower(?)) > 0)
      ORDER BY CASE WHEN instr(lower(v.title), lower(?))=1 THEN 0 ELSE 1 END, v.title COLLATE NOCASE
      LIMIT ?
    ''',
          variables: [
            Variable(profileId),
            Variable(value),
            Variable(value),
            Variable(value),
            Variable(limit.clamp(1, 100).toInt()),
          ],
        )
        .get();
    return [
      for (final row in rows)
        IptvVodItem(
          id: row.read<String>('item_id'),
          sourceId: row.read<String>('source_id'),
          kind: IptvVodKind.values.byName(row.read<String>('kind')),
          categoryId: row.readNullable<String>('category_id'),
          title: row.read<String>('title'),
          plot: row.readNullable<String>('plot'),
          year: row.readNullable<int>('year'),
          durationSeconds: row.readNullable<int>('duration_seconds'),
          rating: row.readNullable<double>('rating'),
          posterUrl: row.readNullable<String>('poster_url'),
          backdropUrl: row.readNullable<String>('backdrop_url'),
          containerExtension: row.read<String>('container_extension'),
        ),
    ];
  }

  Future<List<IptvVodProgress>> vodProgresses(String profileId, String sourceId, IptvVodKind kind) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT p.source_id, p.kind, p.item_id, p.episode_id, p.position_ms, p.duration_ms, p.updated_at, p.finished
      FROM iptv_vod_progress p JOIN iptv_sources s ON s.id=p.source_id
      WHERE s.profile_id=? AND p.source_id=? AND p.kind=?
      ORDER BY p.updated_at DESC LIMIT 2000
    ''',
          variables: [Variable(profileId), Variable(sourceId), Variable(kind.name)],
        )
        .get();
    return [for (final row in rows) _vodProgressFromRow(row)];
  }

  Future<IptvVodProgress?> vodProgress(
    String profileId,
    String sourceId, {
    required IptvVodKind kind,
    required String itemId,
    String episodeId = '',
  }) async {
    final row = await _database
        .customSelect(
          '''
      SELECT p.source_id, p.kind, p.item_id, p.episode_id, p.position_ms, p.duration_ms, p.updated_at, p.finished
      FROM iptv_vod_progress p JOIN iptv_sources s ON s.id=p.source_id
      WHERE s.profile_id=? AND p.source_id=? AND p.kind=? AND p.item_id=? AND p.episode_id=?
    ''',
          variables: [
            Variable(profileId),
            Variable(sourceId),
            Variable(kind.name),
            Variable(itemId),
            Variable(episodeId),
          ],
        )
        .getSingleOrNull();
    return row == null ? null : _vodProgressFromRow(row);
  }

  Future<void> saveVodProgress(
    String profileId, {
    required String sourceId,
    required IptvVodKind kind,
    required String itemId,
    String episodeId = '',
    required int positionMs,
    required int durationMs,
    required bool finished,
  }) async {
    if (itemId.isEmpty || itemId.length > 180 || episodeId.length > 180 || positionMs < 0 || durationMs < 0) {
      throw const IptvImportException('invalid_data');
    }
    if (await _source(profileId, sourceId) == null) {
      throw const IptvImportException('source_missing');
    }
    final duration = durationMs.clamp(0, 14 * 24 * 60 * 60 * 1000).toInt();
    final position = positionMs.clamp(0, 14 * 24 * 60 * 60 * 1000).toInt();
    await _database.customStatement(
      '''
      INSERT INTO iptv_vod_progress(source_id, kind, item_id, episode_id, position_ms, duration_ms, updated_at, finished)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(source_id, kind, item_id, episode_id) DO UPDATE SET
        position_ms=excluded.position_ms, duration_ms=excluded.duration_ms,
        updated_at=excluded.updated_at, finished=excluded.finished
    ''',
      [
        sourceId,
        kind.name,
        itemId,
        episodeId,
        position,
        duration,
        DateTime.now().millisecondsSinceEpoch,
        finished ? 1 : 0,
      ],
    );
  }

  /// Compact, credential-free progress rows suitable for the user's opt-in
  /// Plex account sync. Source endpoints and usernames are represented only
  /// by a SHA-256 fingerprint.
  Future<List<Map<String, Object?>>> exportVodProgressForSync(String profileId) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT p.source_id, p.kind, p.item_id, p.episode_id, p.position_ms, p.duration_ms, p.updated_at, p.finished
      FROM iptv_vod_progress p JOIN iptv_sources s ON s.id=p.source_id
      WHERE s.profile_id=? ORDER BY p.updated_at DESC, p.source_id, p.kind, p.item_id, p.episode_id LIMIT $maxSyncedVodRows
    ''',
          variables: [Variable(profileId)],
        )
        .get();
    final fingerprints = <String, String>{};
    for (final sourceId in rows.map((row) => row.read<String>('source_id')).toSet()) {
      final config = await configuration(profileId, sourceId);
      if (config != null && config.supportsXtreamCatalog) {
        fingerprints[sourceId] = vodProgressSourceFingerprint(config);
      }
    }
    return [
      for (final row in rows)
        if (fingerprints[row.read<String>('source_id')] case final fingerprint?)
          {
            'source': fingerprint,
            'kind': row.read<String>('kind'),
            'item': row.read<String>('item_id'),
            'episode': row.read<String>('episode_id'),
            'position': row.read<int>('position_ms'),
            'duration': row.read<int>('duration_ms'),
            'updated': row.read<int>('updated_at'),
            'finished': row.read<int>('finished') == 1,
          },
    ];
  }

  /// Merge remote progress by timestamp. A more recent local or remote update
  /// wins; provider IDs remain scoped by a matching per-device source.
  Future<void> mergeVodProgressFromSync(
    String profileId,
    List<SyncedVodProgress> rows, {
    required bool Function() isCurrent,
  }) async {
    final fingerprints = <String, List<String>>{};
    for (final source in await sources(profileId)) {
      if (!source.kind.supportsXtreamVod) continue;
      final config = await configuration(profileId, source.id);
      if (config != null && config.supportsXtreamCatalog) {
        (fingerprints[vodProgressSourceFingerprint(config)] ??= []).add(source.id);
      }
    }
    if (!isCurrent()) return;
    await _database.transaction(() async {
      for (final row in rows) {
        for (final id in fingerprints[row.key.source] ?? const <String>[]) {
          await _database.customStatement(
            '''
            INSERT INTO iptv_vod_progress(source_id, kind, item_id, episode_id, position_ms, duration_ms, updated_at, finished)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, kind, item_id, episode_id) DO UPDATE SET
              position_ms=excluded.position_ms, duration_ms=excluded.duration_ms,
              updated_at=excluded.updated_at, finished=excluded.finished
            WHERE excluded.updated_at > iptv_vod_progress.updated_at
               OR (excluded.updated_at = iptv_vod_progress.updated_at AND
                 (excluded.position_ms != iptv_vod_progress.position_ms OR
                  excluded.duration_ms != iptv_vod_progress.duration_ms OR excluded.finished != iptv_vod_progress.finished))
            ''',
            [
              id,
              row.key.kind,
              row.key.item,
              row.key.episode,
              row.position,
              row.duration,
              row.updated,
              row.finished ? 1 : 0,
            ],
          );
        }
        if (!isCurrent()) throw StateError('IPTV progress sync cancelled');
      }
    });
  }

  IptvVodProgress _vodProgressFromRow(QueryRow row) => IptvVodProgress(
    sourceId: row.read<String>('source_id'),
    kind: IptvVodKind.values.byName(row.read<String>('kind')),
    itemId: row.read<String>('item_id'),
    episodeId: row.read<String>('episode_id'),
    positionMs: row.read<int>('position_ms'),
    durationMs: row.read<int>('duration_ms'),
    updatedAt: row.read<int>('updated_at'),
    finished: row.read<int>('finished') == 1,
  );

  Future<Uri> vodPlaybackUri(
    String profileId,
    String sourceId, {
    required String itemId,
    required String extension,
    bool series = false,
  }) async {
    final config = await configuration(profileId, sourceId);
    if (config == null) {
      throw const IptvImportException('source_missing');
    }
    try {
      return config.vodStreamUri(itemId: itemId, extension: extension, series: series);
    } on Object {
      throw const IptvImportException('source_invalid');
    }
  }

  Future<List<IptvLocalRecording>> localRecordings(String profileId) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT id, source_id, channel_id, title, programme_title, file_path, started_at, completed_at,
             duration_seconds, size_bytes, state, error_code
      FROM iptv_recordings WHERE profile_id=? ORDER BY started_at DESC
    ''',
          variables: [Variable(profileId)],
        )
        .get();
    return [
      for (final row in rows)
        IptvLocalRecording(
          id: row.read<String>('id'),
          profileId: profileId,
          sourceId: row.readNullable<String>('source_id'),
          channelId: row.readNullable<String>('channel_id'),
          title: row.read<String>('title'),
          programmeTitle: row.readNullable<String>('programme_title'),
          filePath: row.read<String>('file_path'),
          startedAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('started_at') * 1000),
          completedAt: row.readNullable<int>('completed_at') == null
              ? null
              : DateTime.fromMillisecondsSinceEpoch(row.read<int>('completed_at') * 1000),
          durationSeconds: row.readNullable<int>('duration_seconds'),
          sizeBytes: row.readNullable<int>('size_bytes'),
          state: row.read<String>('state'),
          errorCode: row.readNullable<String>('error_code'),
        ),
    ];
  }

  Future<void> beginLocalRecording(IptvLocalRecording recording) async {
    await _database.runIdentityMutation(
      () => _database.customStatement(
        '''
      INSERT INTO iptv_recordings(id, profile_id, source_id, channel_id, title, programme_title, file_path,
        started_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recording')
    ''',
        [
          recording.id,
          recording.profileId,
          recording.sourceId,
          recording.channelId,
          recording.title,
          recording.programmeTitle,
          recording.filePath,
          recording.startedAt.millisecondsSinceEpoch ~/ 1000,
        ],
      ),
    );
  }

  Future<void> updateLocalRecordingFilePath({
    required String profileId,
    required String recordingId,
    required String filePath,
  }) async {
    await _database.runIdentityMutation(
      () => _database.customStatement('UPDATE iptv_recordings SET file_path=? WHERE profile_id=? AND id=?', [
        filePath,
        profileId,
        recordingId,
      ]),
    );
  }

  Future<String> scheduleLocalRecording(String profileId, LiveTvChannel channel, LiveTvProgram program) async {
    final sourceId = channel.iptvSourceId;
    final channelId = channel.iptvChannelId;
    final startsAt = program.beginsAt;
    final endsAt = program.endsAt;
    if (sourceId == null ||
        channelId == null ||
        startsAt == null ||
        endsAt == null ||
        !IptvLocalRecordingSchedule.canSchedule(
          start: DateTime.fromMillisecondsSinceEpoch(startsAt * 1000),
          end: DateTime.fromMillisecondsSinceEpoch(endsAt * 1000),
          now: DateTime.now(),
        )) {
      throw const IptvImportException('recording_schedule_invalid');
    }
    if (await _source(profileId, sourceId) == null) {
      throw const IptvImportException('source_missing');
    }
    final id = _uuid.v4();
    final stamp = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    try {
      await _database.runIdentityMutation(
        () => _database.transaction(() async {
          final active = await _database
              .customSelect(
                '''
          SELECT id FROM iptv_recording_schedules
          WHERE profile_id=? AND source_id=? AND channel_id=? AND starts_at=?
            AND state IN ('scheduled', 'starting', 'recording')
          LIMIT 1
        ''',
                variables: [Variable(profileId), Variable(sourceId), Variable(channelId), Variable(startsAt)],
              )
              .getSingleOrNull();
          if (active != null) {
            throw const IptvImportException('recording_schedule_exists');
          }
          await _database.customStatement(
            '''
          DELETE FROM iptv_recording_schedules
          WHERE profile_id=? AND source_id=? AND channel_id=? AND starts_at=?
            AND state IN ('completed', 'failed', 'missed', 'cancelled')
        ''',
            [profileId, sourceId, channelId, startsAt],
          );
          await _database.customStatement(
            '''
          INSERT INTO iptv_recording_schedules(
            id, profile_id, source_id, channel_id, title, programme_title, starts_at, ends_at,
            state, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
        ''',
            [
              id,
              profileId,
              sourceId,
              channelId,
              channel.displayName,
              program.displayTitle,
              startsAt,
              endsAt,
              stamp,
              stamp,
            ],
          );
        }),
      );
    } on IptvImportException {
      rethrow;
    } on Object {
      throw const IptvImportException('recording_action_failed');
    }
    return id;
  }

  Future<List<IptvLocalRecordingSchedule>> localRecordingSchedules({String? profileId}) async {
    final rows = await _database
        .customSelect(
          '''
      SELECT id, profile_id, source_id, channel_id, title, programme_title, starts_at, ends_at, state,
             recording_id, error_code, created_at, updated_at
      FROM iptv_recording_schedules
      WHERE (? IS NULL OR profile_id=?) AND state IN ('scheduled', 'starting', 'recording', 'failed', 'missed', 'transfer_pending')
      ORDER BY starts_at, title COLLATE NOCASE
    ''',
          variables: [Variable(profileId), Variable(profileId)],
        )
        .get();
    return [
      for (final row in rows)
        IptvLocalRecordingSchedule(
          id: row.read<String>('id'),
          profileId: row.read<String>('profile_id'),
          sourceId: row.read<String>('source_id'),
          channelId: row.read<String>('channel_id'),
          title: row.read<String>('title'),
          programmeTitle: row.readNullable<String>('programme_title'),
          startsAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('starts_at') * 1000),
          endsAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('ends_at') * 1000),
          state: row.read<String>('state'),
          recordingId: row.readNullable<String>('recording_id'),
          errorCode: row.readNullable<String>('error_code'),
          createdAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('created_at') * 1000),
          updatedAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('updated_at') * 1000),
        ),
    ];
  }

  Future<List<IptvLocalRecordingSchedule>> dueLocalRecordingSchedules() async {
    final rows = await _database.customSelect('''
      SELECT id, profile_id, source_id, channel_id, title, programme_title, starts_at, ends_at, state,
             recording_id, error_code, created_at, updated_at
      FROM iptv_recording_schedules WHERE state='scheduled' ORDER BY starts_at
    ''').get();
    return [
      for (final row in rows)
        IptvLocalRecordingSchedule(
          id: row.read<String>('id'),
          profileId: row.read<String>('profile_id'),
          sourceId: row.read<String>('source_id'),
          channelId: row.read<String>('channel_id'),
          title: row.read<String>('title'),
          programmeTitle: row.readNullable<String>('programme_title'),
          startsAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('starts_at') * 1000),
          endsAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('ends_at') * 1000),
          state: row.read<String>('state'),
          recordingId: row.readNullable<String>('recording_id'),
          errorCode: row.readNullable<String>('error_code'),
          createdAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('created_at') * 1000),
          updatedAt: DateTime.fromMillisecondsSinceEpoch(row.read<int>('updated_at') * 1000),
        ),
    ];
  }

  Future<bool> saveActiveRecordingDeadline(
    IptvLocalRecordingSchedule schedule,
    DateTime previous,
    DateTime next,
  ) async {
    final updated = await _database.runIdentityMutation(
      () => _database.customUpdate(
        "UPDATE iptv_recording_schedules SET ends_at=?, updated_at=? WHERE profile_id=? AND id=? AND state='recording' AND ends_at=?",
        variables: [
          Variable(next.millisecondsSinceEpoch ~/ 1000),
          Variable(DateTime.now().millisecondsSinceEpoch ~/ 1000),
          Variable(schedule.profileId),
          Variable(schedule.id),
          Variable(previous.millisecondsSinceEpoch ~/ 1000),
        ],
      ),
    );
    return updated == 1;
  }

  Future<void> updateLocalRecordingSchedule(
    String scheduleId, {
    required String state,
    String? recordingId,
    String? errorCode,
  }) async {
    if (!const {'scheduled', 'starting', 'recording', 'completed', 'failed', 'missed', 'cancelled'}.contains(state)) {
      throw const IptvImportException('invalid_data');
    }
    final changed = await _database.runIdentityMutation(
      () => _database.customUpdate(
        "UPDATE iptv_recording_schedules SET state=?, recording_id=?, error_code=?, updated_at=? WHERE id=? "
        "AND state NOT IN ('transfer_pending','transferred','cancelled') AND (? != 'starting' OR state='scheduled')",
        variables: [
          Variable(state),
          Variable(recordingId),
          Variable(errorCode),
          Variable(DateTime.now().millisecondsSinceEpoch ~/ 1000),
          Variable(scheduleId),
          Variable(state),
        ],
      ),
    );
    if (state == 'starting' && changed != 1) throw const IptvImportException('recording_schedule_invalid');
  }

  Future<void> recoverLocalRecordingSchedules() async {
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        // Earlier database bootstrap code changed these rows to failed before
        // the scheduler could discover/finalize their journaled capture.
        await _database.customStatement(
          "UPDATE iptv_recordings SET state='interrupted', error_code='app_interrupted' "
          "WHERE state='failed' AND error_code='interrupted'",
        );
        await _database.customStatement(
          '''
        UPDATE iptv_recordings SET state='interrupted', completed_at=?, error_code='app_interrupted'
        WHERE state='recording'
      ''',
          [now],
        );
        await _database.customStatement(
          '''
        UPDATE iptv_recording_schedules SET state='failed', error_code='app_interrupted', updated_at=?
        WHERE state IN ('starting', 'recording')
      ''',
          [now],
        );
      }),
    );
    // Interrupted rows remain playable when their journaled file survived.
    // Older versions journaled the final name instead of the active partial;
    // recover that exact app-owned legacy partial without sweeping/deleting it.
    final interrupted = await _database
        .customSelect("SELECT id, profile_id, file_path FROM iptv_recordings WHERE state='interrupted'")
        .get();
    for (final row in interrupted) {
      final id = row.read<String>('id');
      final profile = row.read<String>('profile_id');
      try {
        var file = File(row.read<String>('file_path'));
        if ((!await file.exists() || await file.length() == 0) &&
            RegExp(r'^[a-f0-9-]{36}$', caseSensitive: false).hasMatch(id)) {
          final legacy = File(p.join(file.parent.path, '.$id.partial.ts'));
          if (await legacy.exists()) {
            file = legacy;
          }
        }
        if (await file.exists()) {
          final archive = await IptvHlsArchive.mediaDirectory(file);
          if (archive != null) {
            await IptvHlsArchive.finalizeInterrupted(file, id);
            if (p.extension(file.path).toLowerCase() != '.m3u8') {
              final recovered = '${file.path}.incomplete.m3u8';
              await updateLocalRecordingFilePath(profileId: profile, recordingId: id, filePath: recovered);
              file = await file.rename(recovered);
            }
          }
          final size = await IptvHlsArchive.retainedBytes(file);
          final mediaDuration = archive == null ? null : await IptvHlsArchive.retainedDuration(file);
          await _database.runIdentityMutation(
            () => _database.customStatement(
              "UPDATE iptv_recordings SET file_path=?, size_bytes=?, duration_seconds=COALESCE(?,duration_seconds), "
              "error_code=CASE WHEN error_code IN ('recovery_io_failed','recovery_data_invalid') "
              "THEN 'app_interrupted' ELSE error_code END WHERE profile_id=? AND id=? AND state='interrupted'",
              [file.path, size, mediaDuration?.inSeconds, profile, id],
            ),
          );
        }
      } on FileSystemException {
        await _markRecordingRecoveryFailure(profile, id, 'recovery_io_failed');
      } on FormatException {
        await _markRecordingRecoveryFailure(profile, id, 'recovery_data_invalid');
      }
    }
  }

  Future<void> _markRecordingRecoveryFailure(String profileId, String recordingId, String code) =>
      _database.runIdentityMutation(
        () => _database.customStatement(
          "UPDATE iptv_recordings SET error_code=? WHERE profile_id=? AND id=? AND state='interrupted'",
          [code, profileId, recordingId],
        ),
      );

  Future<void> cancelLocalRecordingSchedule(String profileId, String scheduleId) async {
    await _database.runIdentityMutation(
      () => _database.customStatement(
        '''
      UPDATE iptv_recording_schedules SET state='cancelled', updated_at=?
      WHERE profile_id=? AND id=? AND state='scheduled'
    ''',
        [DateTime.now().millisecondsSinceEpoch ~/ 1000, profileId, scheduleId],
      ),
    );
  }

  Future<void> finishLocalRecording({
    required String profileId,
    required String recordingId,
    required bool completed,
    int? durationSeconds,
    int? sizeBytes,
    String? errorCode,
  }) async {
    await _database.runIdentityMutation(
      () => _database.customStatement(
        '''
      UPDATE iptv_recordings SET completed_at=?, duration_seconds=?, size_bytes=?, state=?, error_code=?
      WHERE profile_id=? AND id=?
    ''',
        [
          DateTime.now().millisecondsSinceEpoch ~/ 1000,
          durationSeconds,
          sizeBytes,
          completed
              ? 'complete'
              : (sizeBytes ?? 0) > 0
              ? 'interrupted'
              : 'failed',
          errorCode,
          profileId,
          recordingId,
        ],
      ),
    );
  }

  Future<void> deleteLocalRecording(String profileId, String recordingId) async {
    final row = await _database
        .customSelect(
          'SELECT file_path FROM iptv_recordings WHERE profile_id=? AND id=?',
          variables: [Variable(profileId), Variable(recordingId)],
        )
        .getSingleOrNull();
    if (row == null) return;
    final path = row.read<String>('file_path');
    final support = await getApplicationSupportDirectory();
    await IptvHlsArchive.deleteRecordingFiles(
      root: Directory(p.join(support.path, 'iptv-recordings')),
      profileFolders: {iptvRecordingSlug(profileId), iptvManualRecordingFolder(profileId)},
      manifest: File(path),
      recordingId: recordingId,
    );
    // A concurrent recorder may have published its final path in the meantime.
    // Keep that catalog entry available to the next cleanup attempt.
    final removed = await _database.runIdentityMutation(
      () => _database.customUpdate(
        'DELETE FROM iptv_recordings WHERE profile_id=? AND id=? AND file_path=?',
        variables: [Variable(profileId), Variable(recordingId), Variable(path)],
      ),
    );
    if (removed == 0 &&
        await _database
                .customSelect(
                  'SELECT 1 FROM iptv_recordings WHERE profile_id=? AND id=?',
                  variables: [Variable(profileId), Variable(recordingId)],
                )
                .getSingleOrNull() !=
            null) {
      throw const IptvImportException('recording_changed');
    }
  }

  Future<List<IptvDispatcharrRecording>> dispatcharrRecordings(String profileId) async {
    final records = <IptvDispatcharrRecording>[];
    for (final source in await sources(profileId)) {
      if (source.kind != IptvInputKind.dispatcharr) {
        continue;
      }
      final config = await configuration(profileId, source.id);
      if (config?.supportsDispatcharrDvr != true) {
        continue;
      }
      final client = DispatcharrDvrClient(config!);
      try {
        records.addAll(await client.recordings());
      } finally {
        client.close();
      }
    }
    records.sort((a, b) => (a.startedAt ?? DateTime(9999)).compareTo(b.startedAt ?? DateTime(9999)));
    return records;
  }

  Future<List<IptvDispatcharrRule>> dispatcharrRules(String profileId) async {
    final rules = <IptvDispatcharrRule>[];
    for (final source in await sources(profileId)) {
      if (source.kind != IptvInputKind.dispatcharr) {
        continue;
      }
      final config = await configuration(profileId, source.id);
      if (config?.supportsDispatcharrDvr != true) {
        continue;
      }
      final client = DispatcharrDvrClient(config!);
      try {
        rules.addAll(await client.rules());
      } finally {
        client.close();
      }
    }
    return rules;
  }

  Future<({Uri uri, Map<String, String> headers})> dispatcharrRecordingPlayback(
    String profileId,
    IptvDispatcharrRecording recording,
  ) async {
    final config = await configuration(profileId, recording.sourceId);
    if (config?.supportsDispatcharrDvr != true) {
      throw const IptvImportException('dvr_unavailable');
    }
    final client = DispatcharrDvrClient(config!);
    try {
      return (
        uri: client.recordingFileUri(recording.id),
        headers: {'Authorization': 'ApiKey ${config.dispatcharrApiKey.trim()}'},
      );
    } finally {
      client.close();
    }
  }

  Future<void> stopDispatcharrRecording(String profileId, IptvDispatcharrRecording recording) async {
    final config = await configuration(profileId, recording.sourceId);
    if (config?.supportsDispatcharrDvr != true) {
      throw const IptvImportException('dvr_unavailable');
    }
    final client = DispatcharrDvrClient(config!);
    try {
      await client.stopRecording(recording.id);
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } finally {
      client.close();
    }
  }

  Future<void> deleteDispatcharrRecording(String profileId, IptvDispatcharrRecording recording) async {
    final config = await configuration(profileId, recording.sourceId);
    if (config?.supportsDispatcharrDvr != true) {
      throw const IptvImportException('dvr_unavailable');
    }
    final client = DispatcharrDvrClient(config!);
    try {
      await client.deleteRecording(recording.id);
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } finally {
      client.close();
    }
  }

  Future<void> deleteDispatcharrRule(String profileId, IptvDispatcharrRule rule) async {
    final config = await configuration(profileId, rule.sourceId);
    if (config?.supportsDispatcharrDvr != true) {
      throw const IptvImportException('dvr_unavailable');
    }
    final client = DispatcharrDvrClient(config!);
    try {
      await client.deleteRule(rule);
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } finally {
      client.close();
    }
  }

  /// Claim the local timer before issuing a remote write. An uncertain reply
  /// remains recoverable by reading the server schedule, even after restart.
  Future<void> transferLocalRecording(IptvLocalRecordingSchedule job, {String? targetSourceId}) async {
    final pending = job.state == 'transfer_pending' && job.recordingId != null
        ? jsonDecode(job.recordingId!) as Map
        : null;
    final destination = pending?['source'] as String? ?? targetSourceId ?? job.sourceId;
    final config = await configuration(job.profileId, destination);
    if (config == null || !config.supportsDispatcharrDvr) throw const IptvImportException('dvr_unavailable');
    final endpoint = sha256.convert(utf8.encode(config.dispatcharrApiEndpoint.trim())).toString();
    if (pending != null && pending['endpoint'] != endpoint) throw const IptvImportException('dvr_unavailable');
    final client = DispatcharrDvrClient(config);
    final start = job.startsAt.millisecondsSinceEpoch ~/ 1000, end = job.endsAt.millisecondsSinceEpoch ~/ 1000;
    final program = LiveTvProgram(title: job.programmeTitle ?? job.title, beginsAt: start, endsAt: end);
    Future<int> change(String from, String to, String? remoteId) => _database.runIdentityMutation(
      () => _database.customUpdate(
        'UPDATE iptv_recording_schedules SET state=?, recording_id=?, updated_at=? '
        "WHERE id=? AND profile_id=? AND source_id=? AND state=? AND (state!='scheduled' OR starts_at>?)",
        variables: [
          Variable(to),
          Variable(remoteId),
          Variable(DateTime.now().millisecondsSinceEpoch ~/ 1000),
          Variable(job.id),
          Variable(job.profileId),
          Variable(job.sourceId),
          Variable(from),
          Variable(DateTime.now().millisecondsSinceEpoch ~/ 1000),
        ],
      ),
    );
    try {
      final Object remote;
      if (pending != null) {
        remote = pending['channel'] as String;
      } else {
        final channel = (await channels(
          job.profileId,
        )).where((c) => c.iptvSourceId == job.sourceId && c.iptvChannelId == job.channelId).single;
        remote = await client.resolveChannel(channel);
      }
      final claim = jsonEncode({'source': destination, 'endpoint': endpoint, 'channel': remote.toString()});
      Future<IptvDispatcharrRecording?> verify() async {
        final matches = (await client.recordings())
            .where(
              (r) =>
                  r.id.isNotEmpty &&
                  r.channelId == remote.toString() &&
                  r.startedAt?.millisecondsSinceEpoch == start * 1000 &&
                  r.endsAt?.millisecondsSinceEpoch == end * 1000 &&
                  !{'failed', 'cancelled', 'canceled', 'error'}.contains(r.state.toLowerCase()),
            )
            .toList();
        if (matches.length > 1) throw const IptvImportException('recording_schedule_exists');
        return matches.firstOrNull;
      }

      final prior = await verify();
      final claimed = job.state == 'scheduled' && await change('scheduled', 'transfer_pending', claim) == 1;
      if (!claimed && job.state != 'transfer_pending') throw const IptvImportException('recording_schedule_invalid');
      var verified = prior;
      if (claimed && prior == null) {
        try {
          await client.scheduleChannel(remote, program);
        } on SourceException catch (error) {
          if ({400, 401, 403, 404, 405, 422}.contains(error.status)) {
            await change('transfer_pending', 'scheduled', null);
          }
          rethrow;
        }
        verified = await verify();
      }
      if (verified == null) throw const IptvImportException('dvr_unavailable');
      await change('transfer_pending', 'transferred', verified.id);
    } finally {
      client.close();
    }
  }

  Future<void> scheduleDispatcharrRecording(String profileId, LiveTvChannel channel, LiveTvProgram program) async {
    final sourceId = channel.iptvSourceId;
    if (sourceId == null) {
      throw const IptvImportException('dvr_unavailable');
    }
    final config = await configuration(profileId, sourceId);
    if (config?.supportsDispatcharrDvr != true) {
      throw const IptvImportException('dvr_unavailable');
    }
    final client = DispatcharrDvrClient(config!);
    try {
      await client.schedule(channel: channel, program: program);
    } on SourceException catch (error) {
      throw IptvImportException(_iptvCodeForSourceFailure(error));
    } finally {
      client.close();
    }
  }

  Future<bool> supportsDispatcharrDvr(String profileId, String sourceId) async {
    final config = await configuration(profileId, sourceId);
    return config?.supportsDispatcharrDvr == true;
  }

  Future<String?> resolveStream(String profileId, String sourceId, String channelId) async {
    final row = await _database
        .customSelect(
          '''
      SELECT c.stream_url_ciphertext FROM iptv_sources s
      JOIN iptv_channels c ON c.source_id=s.id
      WHERE s.profile_id=? AND s.id=? AND c.channel_id=?
    ''',
          variables: [Variable(profileId), Variable(sourceId), Variable(channelId)],
        )
        .getSingleOrNull();
    if (row == null) {
      return null;
    }
    return CredentialVault.reveal(row.read<String>('stream_url_ciphertext'));
  }

  Future<LiveTvPlaybackSession?> startPlayback(
    String profileId,
    String sourceId,
    String channelId, {
    LiveTvProgram? program,
  }) async {
    final primary = await configuration(profileId, sourceId);
    if (primary == null) return null;
    if (program != null || primary.backupSourceIds.isEmpty) {
      return _openChannel(profileId, sourceId, channelId, program: program);
    }
    final ids = [sourceId, ...primary.backupSourceIds.where((id) => id != sourceId).toSet()];
    var index = 0;
    Future<LiveTvPlaybackSession?> next() async {
      final original = (await channels(
        profileId,
        sourceId: sourceId,
      )).where((c) => c.iptvChannelId == channelId).firstOrNull;
      if (original == null) return null;
      final latest = await configuration(profileId, sourceId);
      if (latest == null) return null;
      for (var attempts = 0; attempts < ids.length; attempts++) {
        final candidateId = ids[index++ % ids.length];
        if (candidateId != sourceId && !latest.backupSourceIds.contains(candidateId)) continue;
        LiveTvChannel? candidate = original;
        if (candidateId != sourceId) {
          candidate = matchIptvBackup(original, await channels(profileId, sourceId: candidateId));
        }
        if (candidate == null) continue;
        try {
          final session = await _openChannel(profileId, candidateId, candidate.iptvChannelId!, reopen: next);
          if (session != null) return session;
        } on IptvImportException {
          continue;
        } on SourceException {
          continue;
        }
      }
      return null;
    }

    return next();
  }

  Future<LiveTvPlaybackSession?> _openChannel(
    String profileId,
    String sourceId,
    String channelId, {
    LiveTvProgram? program,
    Future<LiveTvPlaybackSession?> Function()? reopen,
  }) async {
    final config = await configuration(profileId, sourceId);
    if (config == null) {
      return null;
    }
    final row = await _database
        .customSelect(
          '''
      SELECT c.stream_url_ciphertext, c.catchup_source_ciphertext, c.catchup_days, c.stream_headers_ciphertext
      FROM iptv_sources s JOIN iptv_channels c ON c.source_id=s.id
      WHERE s.profile_id=? AND s.id=? AND c.channel_id=?
    ''',
          variables: [Variable(profileId), Variable(sourceId), Variable(channelId)],
        )
        .getSingleOrNull();
    if (row == null) {
      return null;
    }
    final streamCiphertext = row.read<String>('stream_url_ciphertext');
    final catchupCiphertext = row.readNullable<String>('catchup_source_ciphertext');
    final catchupDays = row.readNullable<int>('catchup_days');
    final headersCiphertext = row.readNullable<String>('stream_headers_ciphertext');
    final headersJson = headersCiphertext == null ? null : await CredentialVault.reveal(headersCiphertext);
    if (headersCiphertext != null && headersJson == null) {
      throw const IptvImportException('credential_unavailable');
    }
    final headers = <String, String>{};
    if (headersJson != null) {
      try {
        final decoded = jsonDecode(headersJson);
        if (decoded is Map) {
          for (final entry in decoded.entries) {
            if (entry.key is! String || entry.value is! String) {
              continue;
            }
            final key = (entry.key as String).toLowerCase();
            if (key == 'user-agent' || key == 'referer' || key == 'origin') {
              headers[key == 'user-agent'
                      ? 'User-Agent'
                      : key == 'referer'
                      ? 'Referer'
                      : 'Origin'] =
                  entry.value as String;
            }
          }
        }
      } on FormatException {
        throw const IptvImportException('invalid_data');
      }
    }
    final catchupTemplate = catchupCiphertext == null ? null : await CredentialVault.reveal(catchupCiphertext);
    if (catchupCiphertext != null && catchupTemplate == null) {
      throw const IptvImportException('credential_unavailable');
    }
    Future<String?> resolve() async {
      if (program != null && catchupTemplate != null) {
        final start = program.beginsAt;
        final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
        if (start == null || start > now || (catchupDays != null && now - start > catchupDays * 86400)) {
          throw const IptvImportException('catchup_unavailable');
        }
        return _catchupUrl(catchupTemplate, program, config);
      }
      final value = await CredentialVault.reveal(streamCiphertext);
      if (value == null) {
        return null;
      }
      if (config.kind == IptvInputKind.stalker) {
        final command = _stalkerCommand(value);
        final client = StalkerPortalClient(config);
        try {
          return await client.createLink(command);
        } finally {
          client.close();
        }
      }
      try {
        return UniversalMediaAddress.parse(value, allowHttp: config.allowHttp, allowMpvProtocols: true).toString();
      } on SourceException {
        throw const IptvImportException('invalid_stream_address');
      }
    }

    final url = await resolve();
    if (url == null) {
      return null;
    }
    return IptvLiveTvPlaybackSession(
      initialUrl: url,
      refreshUrl: resolve,
      reopen: reopen,
      streamHeaders: Map.unmodifiable(headers),
      localTimeShiftEnabled: config.localTimeShiftEnabled,
      isCatchup: program != null && catchupTemplate != null,
      programInfo: program == null
          ? LiveProgramInfo.none
          : LiveProgramInfo(
              beginsAt: program.beginsAt,
              durationMs: program.durationMinutes == 0 ? null : program.durationMinutes * 60 * 1000,
            ),
    );
  }

  String _stalkerCommand(String stored) {
    const prefix = 'stalker:';
    if (!stored.startsWith(prefix)) {
      throw const IptvImportException('invalid_stream_address');
    }
    try {
      final payload = sourceMap(jsonDecode(stored.substring(prefix.length)));
      final command = payload['cmd'];
      if (command is! String || command.trim().isEmpty || command.length > 8192) {
        throw const IptvImportException('invalid_stream_address');
      }
      return command;
    } on IptvImportException {
      rethrow;
    } on Object {
      throw const IptvImportException('invalid_stream_address');
    }
  }

  Future<void> deleteSource(String profileId, String sourceId) async {
    await _database.runIdentityMutation(
      () => _database.customUpdate(
        'DELETE FROM iptv_sources WHERE profile_id=? AND id=?',
        variables: [Variable(profileId), Variable(sourceId)],
      ),
    );
  }

  Future<void> deleteAllProfiles() async {
    final owners = await _database
        .customSelect('SELECT profile_id FROM iptv_sources UNION SELECT profile_id FROM iptv_recordings')
        .get();
    for (final owner in owners) {
      await deleteProfile(owner.read<String>('profile_id'));
    }
  }

  Future<void> deleteProfile(String profileId) async {
    final recordings = await localRecordings(profileId);
    Object? failure;
    for (final recording in recordings) {
      try {
        await deleteLocalRecording(profileId, recording.id);
      } on Object catch (error) {
        // Keep the row (including its path) so a retry can finish cleanup.
        failure ??= error;
      }
    }
    if (failure != null) throw failure;
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        final remaining = await _database
            .customSelect('SELECT 1 FROM iptv_recordings WHERE profile_id=? LIMIT 1', variables: [Variable(profileId)])
            .getSingleOrNull();
        if (remaining != null) throw const IptvImportException('recording_changed');
        await _database.customUpdate('DELETE FROM iptv_sources WHERE profile_id=?', variables: [Variable(profileId)]);
      }),
    );
  }

  Future<int> refresh(String profileId, String sourceId) async {
    final source = await _source(profileId, sourceId);
    if (source == null) {
      throw const IptvImportException('source_missing');
    }
    final config = await configuration(profileId, sourceId);
    if (config == null) {
      throw const IptvImportException('source_missing');
    }

    var expected = source;
    final transport = SourceTransport(allowHttp: config.allowHttp);
    StalkerPortalClient? stalker;
    try {
      late final List<IptvPlaylistEntry> playlistEntries;
      Uri? playlistGuideUri;
      if (config.kind == IptvInputKind.stalker) {
        stalker = StalkerPortalClient(config);
        playlistEntries = await stalker.channels();
      } else {
        final playlist = await _loadChannelCatalog(config, transport);
        if (playlist.isHls) throw const IptvImportException('playlist_is_stream');
        playlistEntries = playlist.entries;
        playlistGuideUri = playlist.guideUri;
      }

      final identities = config.kind == IptvInputKind.stalker
          ? <String, String>{}
          : await _feedIdentities(source.id, playlistEntries);
      final channels = [for (final entry in playlistEntries) entry.withId(identities[entry.id] ?? entry.id)];

      // Make tuning URLs usable immediately. A slow guide may still be loading
      // when another client or the recording scheduler opens a channel.
      expected = await _publishLineup(source, channels, identities);

      final guideUris = <Uri>[];
      final primaryGuideUri = config.guideUri() ?? playlistGuideUri;
      if (primaryGuideUri != null) {
        guideUris.add(primaryGuideUri);
      }
      for (final address in config.additionalGuideEndpoints) {
        final uri = Uri.parse(address.trim());
        if (!guideUris.contains(uri)) {
          guideUris.add(uri);
        }
      }
      final guideSources = <List<IptvXmltvProgramme>>[];
      var programmeCount = 0;
      final guideLogos = <String, String>{};
      final freshGuides = <String, IptvXmltvParseResult>{};
      final guideKeys = <String>{};
      var guideFailed = false;
      var retainPublished = false;
      void mergeGuide(IptvXmltvParseResult guide) {
        if (guide.programmes.isNotEmpty) guideSources.add(guide.programmes);
        programmeCount += guide.programmes.length;
        for (final entry in guide.channelLogos.entries) {
          guideLogos.putIfAbsent(entry.key, () => entry.value);
        }
      }

      for (final guideUri in guideUris) {
        final key = sha256.convert(utf8.encode(guideUri.toString())).toString();
        guideKeys.add(key);
        try {
          final guide = await IptvXmltvParser.fetch(transport, guideUri);
          if ((guide.programmes.isEmpty && guide.skipped != 0) ||
              programmeCount + guide.programmes.length > IptvXmltvParser.maxProgrammes) {
            throw const IptvImportException('invalid_data');
          }
          freshGuides[key] = guide;
          mergeGuide(guide);
        } on Object {
          guideFailed = true;
          final cached = await _cachedGuide(source.id, key, IptvXmltvParser.maxProgrammes - programmeCount);
          if (cached.programmes.isEmpty) retainPublished = true;
          mergeGuide(cached);
        }
      }
      if (guideFailed) {
        // v37 stored a single merged snapshot without the original feed ID.
        guideKeys.add('');
        mergeGuide(await _cachedGuide(source.id, '', IptvXmltvParser.maxProgrammes - programmeCount));
      }
      final overrideRows = await _database
          .customSelect(
            'SELECT channel_id, guide_id_override FROM iptv_channel_overrides WHERE profile_id=? AND source_id=?',
            variables: [Variable(profileId), Variable(sourceId)],
          )
          .get();
      final guideIdOverrides = {
        for (final row in overrideRows)
          row.read<String>('channel_id'): row.readNullable<String>('guide_id_override')?.trim(),
      };
      final effectiveTvgIds = <String, String?>{
        for (final channel in channels)
          channel.id: guideIdOverrides[channel.id]?.isNotEmpty == true ? guideIdOverrides[channel.id] : channel.tvgId,
      };
      final tvgToChannelIds = <String, List<String>>{};
      for (final channel in channels) {
        final tvgId = effectiveTvgIds[channel.id];
        if (tvgId != null) {
          tvgToChannelIds.putIfAbsent(tvgId, () => []).add(channel.id);
        }
      }
      final mappedPrograms = _mapGuideProgrammes(mergeIptvGuideProgrammes(guideSources), tvgToChannelIds);
      final fallbackLogos = <String, String>{
        for (final channel in channels)
          if (channel.logoUrl == null)
            if (guideLogos[effectiveTvgIds[channel.id]] case final String logo) channel.id: logo,
      };
      await _publishGuide(
        expected,
        mappedPrograms,
        guideKeys,
        freshGuides,
        fallbackLogos,
        guideFailed,
        retainPublished,
      );
      return channels.length;
    } on IptvImportException catch (error) {
      await _recordError(expected, error.code);
      rethrow;
    } on SourceException catch (error) {
      final code = switch (error.reason) {
        SourceFailure.authentication => 'authentication',
        SourceFailure.permission => 'permission',
        SourceFailure.invalidData => 'invalid_data',
        SourceFailure.cancelled => 'cancelled',
        _ => 'provider_unavailable',
      };
      await _recordError(expected, code);
      throw IptvImportException(code);
    } on Object {
      await _recordError(expected, 'provider_unavailable');
      throw const IptvImportException('provider_unavailable');
    } finally {
      stalker?.close();
      transport.close();
    }
  }

  Future<IptvPlaylistParseResult> _loadChannelCatalog(IptvSourceConfiguration config, SourceTransport transport) async {
    final uri = config.playlistUri();
    if (config.kind == IptvInputKind.m3uFile) {
      final row = await _database
          .customSelect(
            'SELECT playlist_ciphertext FROM iptv_sources WHERE id=? AND profile_id=?',
            variables: [Variable(config.id), Variable(config.profileId)],
          )
          .getSingleOrNull();
      final ciphertext = row?.readNullable<String>('playlist_ciphertext');
      if (ciphertext == null) throw const IptvImportException('playlist_missing');
      final contents = await CredentialVault.reveal(ciphertext);
      if (contents == null) throw const IptvImportException('credential_unavailable');
      return IptvPlaylistParser.load(contents, base: uri, allowHttp: config.allowHttp);
    }

    Future<IptvPlaylistParseResult> readPlaylist() async {
      final document = await transport.textDocument(
        'GET',
        uri,
        limit: maxPlaylistBytes,
        redirects: true,
        headers: const {'Accept': 'audio/x-mpegurl, application/vnd.apple.mpegurl, text/plain, */*'},
      );
      return IptvPlaylistParser.load(document.body, base: document.uri, allowHttp: config.allowHttp);
    }

    if (config.kind != IptvInputKind.xtream) return readPlaylist();
    final apiFirst = Uri.parse(config.endpoint).pathSegments.lastOrNull?.toLowerCase() == 'player_api.php';
    Future<IptvPlaylistParseResult> readLiveApi() => IptvXtreamLiveCatalog.load(transport, config);
    try {
      final result = await (apiFirst ? readLiveApi() : readPlaylist());
      if (!result.isHls) return result;
    } on SourceDocumentLimit {
      if (apiFirst) rethrow;
      // A combined M3U may be too large while the provider's live-only list fits.
    } on Object {
      if (transport.isClosed) throw const SourceException(SourceFailure.cancelled);
    }
    return apiFirst ? readPlaylist() : readLiveApi();
  }

  // Reuse each previously published channel ID when its feed can be matched.
  // Existing favorites, locks, recents, and pending timers then need no rewrite.

  Future<IptvXmltvParseResult> _cachedGuide(String sourceId, String key, int limit) async {
    final args = [Variable(sourceId), Variable(key)];
    final rows = await _database
        .customSelect(
          'SELECT * FROM iptv_guide_programmes WHERE source_id=? AND guide_key=? LIMIT ?',
          variables: [...args, Variable(limit)],
        )
        .get();
    final logos = await _database
        .customSelect('SELECT tvg_id,logo_url FROM iptv_guide_logos WHERE source_id=? AND guide_key=?', variables: args)
        .get();
    return IptvXmltvParseResult(
      skipped: 0,
      channelLogos: {for (final row in logos) row.read<String>('tvg_id'): row.read<String>('logo_url')},
      programmes: [
        for (final row in rows)
          IptvXmltvProgramme(
            channelTvgId: row.read<String>('tvg_id'),
            beginsAt: row.read<int>('begins_at'),
            endsAt: row.read<int>('ends_at'),
            title: row.read<String>('title'),
            summary: row.readNullable<String>('summary'),
            category: row.readNullable<String>('category'),
            episodeId: row.readNullable<String>('episode_id'),
            thumb: row.readNullable<String>('thumb_url'),
          ),
      ],
    );
  }

  Future<void> _publishGuideCache(String source, Set<String> keys, Map<String, IptvXmltvParseResult> fresh) async {
    for (final table in ['iptv_guide_logos', 'iptv_guide_programmes']) {
      final remaining = keys.difference(fresh.keys.toSet());
      final keep = remaining.isEmpty ? '' : ' AND guide_key NOT IN (${List.filled(remaining.length, "?").join(",")})';
      await _database.customStatement('DELETE FROM $table WHERE source_id=?$keep', [source, ...remaining]);
    }
    for (final entry in fresh.entries) {
      await _insertRows(
        table: 'iptv_guide_programmes',
        columns: const [
          'source_id',
          'guide_key',
          'tvg_id',
          'begins_at',
          'ends_at',
          'title',
          'summary',
          'category',
          'episode_id',
          'thumb_url',
        ],
        rows: [
          for (final p in entry.value.programmes)
            [
              source,
              entry.key,
              p.channelTvgId,
              p.beginsAt,
              p.endsAt,
              p.title,
              p.summary,
              p.category,
              p.episodeId,
              p.thumb,
            ],
        ],
      );
      await _insertRows(
        table: 'iptv_guide_logos',
        columns: const ['source_id', 'guide_key', 'tvg_id', 'logo_url'],
        rows: [
          for (final logo in entry.value.channelLogos.entries) [source, entry.key, logo.key, logo.value],
        ],
      );
    }
  }

  /// Fans out shared guide IDs and lets the first configured guide win an
  /// overlapping channel/start slot. Later guides can fill gaps without
  /// duplicating schedule rows or violating the programme table's primary key.
  List<IptvProgrammeSnapshot> _mapGuideProgrammes(
    Iterable<IptvXmltvProgramme> programmes,
    Map<String, List<String>> tvgToChannelIds,
  ) {
    final seenChannelStarts = <(String, int)>{};
    final mapped = <IptvProgrammeSnapshot>[];
    for (final programme in programmes) {
      for (final channelId in tvgToChannelIds[programme.channelTvgId] ?? const <String>[]) {
        if (!seenChannelStarts.add((channelId, programme.beginsAt))) continue;
        mapped.add(
          IptvProgrammeSnapshot(
            channelId: channelId,
            beginsAt: programme.beginsAt,
            endsAt: programme.endsAt,
            title: programme.title,
            summary: programme.summary,
            category: programme.category,
            episodeId: programme.episodeId,
            thumb: programme.thumb,
          ),
        );
      }
    }
    return mapped;
  }

  Future<void> _publishGuide(
    _StoredIptvSource expected,
    List<IptvProgrammeSnapshot> programmes,
    Set<String> guideKeys,
    Map<String, IptvXmltvParseResult> freshGuides,
    Map<String, String> fallbackLogos,
    bool guideFailed,
    bool retainPublished,
  ) async {
    await _database.runIdentityMutation(
      () => _database.transaction(() async {
        final current = await _source(expected.profileId, expected.id);
        if (current == null || current.revision != expected.revision || current.generation != expected.generation) {
          throw const IptvImportException('source_changed');
        }
        await _publishGuideCache(expected.id, guideKeys, freshGuides);
        if (retainPublished) {
          // A failed feed has no per-feed snapshot. Retain existing listings
          // outside known coverage without resurrecting airings replaced there.
          await _database.batch((batch) {
            for (final item in programmes) {
              batch.customStatement(
                'DELETE FROM iptv_programmes WHERE source_id=? AND channel_id=? AND ends_at>? AND begins_at<?',
                [expected.id, item.channelId, item.beginsAt, item.endsAt],
              );
            }
          });
        } else {
          await _database.customStatement('DELETE FROM iptv_programmes WHERE source_id=?', [expected.id]);
        }
        final programmeRows = [
          for (final item in programmes)
            [
              expected.id,
              item.channelId,
              item.beginsAt,
              item.endsAt,
              item.title,
              item.summary,
              item.category,
              item.episodeId,
              item.thumb,
            ],
        ];
        await _insertRows(
          table: 'iptv_programmes',
          columns: const [
            'source_id',
            'channel_id',
            'begins_at',
            'ends_at',
            'title',
            'summary',
            'category',
            'episode_id',
            'thumb_url',
          ],
          rows: programmeRows,
        );
        if (fallbackLogos.isNotEmpty) {
          await _database.batch((batch) {
            for (final entry in fallbackLogos.entries) {
              batch.customStatement(
                'UPDATE iptv_channels SET logo_url=? WHERE source_id=? AND channel_id=? AND logo_url IS NULL',
                [entry.value, expected.id, entry.key],
              );
            }
          });
        }
        await seriesRules.reconcile(expected.profileId, expected.id);
        await _database.customStatement('UPDATE iptv_sources SET last_error_code=? WHERE id=?', [
          guideFailed ? 'guide_unavailable' : null,
          expected.id,
        ]);
      }),
    );
  }

  Future<void> _insertRows({
    required String table,
    required List<String> columns,
    required List<List<Object?>> rows,
    bool replace = false,
  }) async {
    if (rows.isEmpty) {
      return;
    }
    final rowsPerStatement = (900 ~/ columns.length).clamp(1, 100).toInt();
    for (var start = 0; start < rows.length; start += rowsPerStatement) {
      final chunk = rows.skip(start).take(rowsPerStatement).toList(growable: false);
      final rowPlaceholder = '(${List.filled(columns.length, '?').join(', ')})';
      final valuesClause = List.filled(chunk.length, rowPlaceholder).join(', ');
      final values = <Object?>[for (final row in chunk) ...row];
      await _database.customStatement(
        "INSERT OR ${replace ? 'REPLACE' : 'IGNORE'} INTO $table(${columns.join(', ')}) VALUES $valuesClause",
        values,
      );
    }
  }

  Future<Object?> _xtreamJson(SourceTransport transport, Uri uri) async {
    transport.validate(uri);
    final body = await transport.text(
      'GET',
      uri,
      limit: maxPlaylistBytes,
      redirects: true,
      headers: const {'Accept': 'application/json, text/plain, */*'},
    );
    try {
      return body.length < 256 * 1024 ? jsonDecode(body) : await Isolate.run(() => jsonDecode(body));
    } on FormatException {
      throw const IptvImportException('invalid_data');
    }
  }

  List<dynamic> _xtreamRows(Object? value) {
    if (value is List) {
      return value;
    }
    if (value is Map) {
      final map = sourceMap(value);
      for (final key in const ['data', 'results', 'streams', 'categories', 'series']) {
        if (map[key] is List) {
          return map[key] as List;
        }
      }
    }
    throw const IptvImportException('invalid_data');
  }

  String? _valueString(Map<String, dynamic> row, List<String> keys) {
    for (final key in keys) {
      final value = row[key];
      if (value == null) {
        continue;
      }
      final text = '$value'.trim();
      if (text.isNotEmpty && text.toLowerCase() != 'null' && text != '0') {
        return text;
      }
    }
    return null;
  }

  String? _firstStringValue(Map<String, dynamic> row, List<String> keys) {
    for (final key in keys) {
      final value = row[key];
      if (value is String && value.trim().isNotEmpty) {
        return value.trim();
      }
      if (value is List && value.isNotEmpty && value.first is String) {
        return (value.first as String).trim();
      }
    }
    return null;
  }

  int? _valueInt(Map<String, dynamic> row, List<String> keys) {
    for (final key in keys) {
      final value = row[key];
      if (value is num) {
        return value.toInt();
      }
      if (value is String) {
        final parsed = int.tryParse(value.trim());
        if (parsed != null) {
          return parsed;
        }
      }
    }
    return null;
  }

  double? _valueDouble(Map<String, dynamic> row, List<String> keys) {
    for (final key in keys) {
      final value = row[key];
      if (value is num && value.isFinite) {
        return value.toDouble();
      }
      if (value is String) {
        final parsed = double.tryParse(value.trim());
        if (parsed != null && parsed.isFinite) {
          return parsed;
        }
      }
    }
    return null;
  }

  int? _durationSeconds(String? value) {
    if (value == null) {
      return null;
    }
    final parts = value.split(':').map(int.tryParse).toList();
    if (parts.length != 3 || parts.any((part) => part == null || part < 0)) {
      return int.tryParse(value);
    }
    return parts[0]! * 3600 + parts[1]! * 60 + parts[2]!;
  }

  String? _safeCatalogText(String? value, int maxLength) {
    final clean = value?.replaceAll(RegExp(r'[\x00-\x1f\x7f]'), '').trim();
    if (clean == null || clean.isEmpty) {
      return null;
    }
    return clean.length <= maxLength ? clean : clean.substring(0, maxLength);
  }

  String? _safeVodArtwork(String? raw, IptvSourceConfiguration config) {
    if (raw == null || raw.length > UniversalMediaAddress.maxLength) {
      return null;
    }
    try {
      final uri = UniversalMediaAddress.parse(raw, allowHttp: config.allowHttp, base: config.xtreamBaseUri);
      return uri.toString();
    } on Object {
      return null;
    }
  }

  String _iptvSafeExtension(String raw) {
    final value = raw.trim().toLowerCase().replaceFirst(RegExp(r'^\.'), '');
    return RegExp(r'^[a-z0-9]{1,8}$').hasMatch(value) ? value : 'mp4';
  }

  String _iptvCodeForSourceFailure(SourceException error) => switch (error.reason) {
    SourceFailure.authentication => 'authentication',
    SourceFailure.permission => 'permission',
    SourceFailure.invalidData => 'invalid_data',
    SourceFailure.unsupported => 'unsupported',
    _ => 'provider_unavailable',
  };

  Future<List<R>> _mapInBatches<T, R>(List<T> values, Future<R> Function(T) resolve) async {
    const batchSize = 64;
    final result = <R>[];
    for (var start = 0; start < values.length; start += batchSize) {
      result.addAll(await Future.wait(values.skip(start).take(batchSize).map(resolve)));
    }
    return result;
  }

  Future<_StoredIptvSource?> _source(String profileId, String sourceId) async {
    final row = await _database
        .customSelect(
          '''
      SELECT id, profile_id, display_name, kind, revision, generation, created_at, last_success_at, last_error_code
      FROM iptv_sources WHERE profile_id=? AND id=?
    ''',
          variables: [Variable(profileId), Variable(sourceId)],
        )
        .getSingleOrNull();
    if (row == null) {
      return null;
    }
    return _StoredIptvSource(
      id: row.read<String>('id'),
      profileId: row.read<String>('profile_id'),
      displayName: row.read<String>('display_name'),
      kind: row.read<String>('kind'),
      revision: row.read<int>('revision'),
      generation: row.read<int>('generation'),
      createdAt: row.read<int>('created_at'),
      lastSuccessAt: row.readNullable<int>('last_success_at'),
      lastErrorCode: row.readNullable<String>('last_error_code'),
    );
  }

  String _catchupUrl(String template, LiveTvProgram program, IptvSourceConfiguration config) {
    final start = program.beginsAt;
    final end = program.endsAt;
    if (start == null || end == null || end <= start) {
      throw const IptvImportException('catchup_unavailable');
    }
    final duration = end - start;
    final startTime = _catchupTimestamp(start);
    final endTime = _catchupTimestamp(end);
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    final values = <String, String>{
      '{utcstart}': '$start',
      '{utcend}': '$end',
      '{utc}': '$start',
      '{lutc}': '$now',
      '{duration}': '$duration',
      '{start}': startTime,
      '{end}': endTime,
      '%s': '$start',
      '%Y': startTime.substring(0, 4),
      '%m': startTime.substring(4, 6),
      '%d': startTime.substring(6, 8),
      '%H': startTime.substring(8, 10),
      '%M': startTime.substring(10, 12),
      '%S': startTime.substring(12, 14),
    };
    var expanded = template;
    for (final entry in values.entries) {
      expanded = expanded.replaceAll(entry.key, entry.value);
    }
    try {
      return UniversalMediaAddress.parse(
        expanded,
        allowHttp: config.allowHttp,
        allowMpvProtocols: true,
        base: config.playlistUri(),
      ).toString();
    } on SourceException {
      throw const IptvImportException('catchup_unavailable');
    } on FormatException {
      throw const IptvImportException('catchup_unavailable');
    }
  }

  String _catchupTimestamp(int epochSeconds) {
    final date = DateTime.fromMillisecondsSinceEpoch(epochSeconds * 1000, isUtc: true);
    String two(int value) => value.toString().padLeft(2, '0');
    return '${date.year}${two(date.month)}${two(date.day)}${two(date.hour)}${two(date.minute)}${two(date.second)}';
  }

  Future<void> _recordError(_StoredIptvSource expected, String code) async {
    try {
      await _database.runIdentityMutation(
        () => _database.customStatement(
          '''
          UPDATE iptv_sources SET last_error_code=?
          WHERE profile_id=? AND id=? AND revision=? AND generation=?
        ''',
          [code, expected.profileId, expected.id, expected.revision, expected.generation],
        ),
      );
    } on Object {
      // A removed, updated or refreshed source is not changed by a late failure.
    }
  }

  IptvInputKind _kind(String value) {
    try {
      return IptvInputKind.values.byName(value);
    } on ArgumentError {
      throw const IptvImportException('source_invalid');
    }
  }

  void _validate(IptvSourceConfiguration config) {
    if (config.profileId.isEmpty || config.displayName.isEmpty || config.displayName.length > 80) {
      throw const IptvImportException('source_invalid');
    }
    for (final value in [
      if (config.kind != IptvInputKind.m3uFile || config.endpoint.isNotEmpty) config.endpoint,
      if (config.guideEndpoint.isNotEmpty) config.guideEndpoint,
      ...config.additionalGuideEndpoints,
    ]) {
      final uri = Uri.tryParse(value);
      if (uri == null || uri.host.isEmpty || uri.userInfo.isNotEmpty || uri.hasFragment || value.length > 4096) {
        throw const IptvImportException('source_invalid');
      }
      if (uri.scheme != 'https' && !(config.allowHttp && uri.scheme == 'http')) {
        throw const IptvImportException('http_not_allowed');
      }
      if (value == config.endpoint &&
          config.kind == IptvInputKind.xtream &&
          uri.queryParameters.keys.any((key) => key == 'password')) {
        throw const IptvImportException('source_invalid');
      }
    }
    if (config.kind == IptvInputKind.xtream && (config.username.isEmpty || config.password.isEmpty)) {
      throw const IptvImportException('source_invalid');
    }
    if (config.kind == IptvInputKind.dispatcharr && config.username.isNotEmpty != config.password.isNotEmpty) {
      throw const IptvImportException('source_invalid');
    }
    if (config.kind == IptvInputKind.stalker &&
        !RegExp(r'^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$').hasMatch(config.macAddress.trim().toUpperCase())) {
      throw const IptvImportException('source_invalid');
    }
    if (config.additionalGuideEndpoints.length > 8) {
      throw const IptvImportException('source_invalid');
    }
    final apiEndpoint = config.dispatcharrApiEndpoint.trim();
    final apiKey = config.dispatcharrApiKey.trim();
    if (apiEndpoint.isNotEmpty != apiKey.isNotEmpty) {
      throw const IptvImportException('source_invalid');
    }
    if (apiEndpoint.isNotEmpty) {
      if (config.kind != IptvInputKind.dispatcharr) {
        throw const IptvImportException('source_invalid');
      }
      final uri = Uri.tryParse(apiEndpoint);
      if (uri == null || uri.host.isEmpty || uri.userInfo.isNotEmpty || uri.hasFragment || uri.hasQuery) {
        throw const IptvImportException('source_invalid');
      }
      if (uri.scheme != 'https' && !(config.allowHttp && uri.scheme == 'http')) {
        throw const IptvImportException('http_not_allowed');
      }
    }
  }
}

String iptvGroupLockKey(String? title) => title?.trim().toLowerCase() ?? '';
