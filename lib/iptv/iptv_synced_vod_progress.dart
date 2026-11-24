import 'dart:convert';

import 'package:collection/collection.dart';
import 'package:crypto/crypto.dart';

import '../profiles/profile.dart';
import 'iptv_models.dart';

const maxSyncedVodRows = 1000;
const _maxPosition = 14 * 24 * 60 * 60 * 1000;

/// Use one shared Plex setting per profile so devices updating different
/// profiles never have to replace each other's history.
String vodProgressSettingId(Profile profile) {
  final identity = profile.isPlexHome ? profile.plexHomeUserUuid : profile.id;
  if (identity == null || identity.isEmpty) throw const FormatException('Missing IPTV sync identity');
  final digest = sha256.convert(utf8.encode(jsonEncode([profile.kind.name, identity])));
  return 'plezy_iptv_vod_progress_v2_$digest';
}

String vodProgressSourceFingerprint(IptvSourceConfiguration config) {
  final base = config.xtreamBaseUri;
  final key = [config.kind.name, base.origin, base.path, config.username];
  return sha256.convert(utf8.encode(jsonEncode(key))).toString();
}

typedef SyncedVodKey = ({String source, String kind, String item, String episode});

class SyncedVodProgress {
  const SyncedVodProgress(this.key, this.position, this.duration, this.updated, this.finished);

  final SyncedVodKey key;
  final int position, duration, updated;
  final bool finished;

  static final _fingerprint = RegExp(r'^[0-9a-f]{64}$');

  factory SyncedVodProgress.fromJson(Object? value) {
    if (value
        case {
          'source': final String source,
          'kind': final String kind,
          'item': final String item,
          'episode': final String episode,
          'position': final int position,
          'duration': final int duration,
          'updated': final int updated,
          'finished': final bool finished,
        }
        when _fingerprint.hasMatch(source) &&
            (kind == 'movie' || kind == 'series') &&
            item.isNotEmpty &&
            item.length <= 180 &&
            episode.length <= 180 &&
            position >= 0 &&
            position <= _maxPosition &&
            duration >= 0 &&
            duration <= _maxPosition &&
            updated >= 0) {
      return SyncedVodProgress(
        (source: source, kind: kind, item: item, episode: episode),
        position,
        duration,
        updated,
        finished,
      );
    }
    throw const FormatException('Invalid IPTV progress row');
  }

  Map<String, Object?> toJson() => {
    'source': key.source,
    'kind': key.kind,
    'item': key.item,
    'episode': key.episode,
    'position': position,
    'duration': duration,
    'updated': updated,
    'finished': finished,
  };

  static List<SyncedVodProgress> decode(Object? raw) {
    if (raw is! String) throw const FormatException('Missing IPTV progress setting');
    final Object? decoded;
    try {
      decoded = jsonDecode(raw);
    } on FormatException {
      throw const FormatException('Unreadable IPTV progress setting');
    }
    if (decoded case {'v': 2, 'rows': final List rows} when rows.length <= maxSyncedVodRows) {
      final progress = rows.map(SyncedVodProgress.fromJson).toList();
      if (progress.map((row) => row.key).toSet().length == progress.length) return progress;
    }
    throw const FormatException('Invalid IPTV progress setting');
  }

  static List<SyncedVodProgress> merge(List<SyncedVodProgress> remote, Iterable<SyncedVodProgress> local) {
    final newest = <SyncedVodKey, SyncedVodProgress>{};
    for (final row in [...remote, ...local]) {
      final other = newest[row.key];
      if (other == null || row.updated > other.updated) newest[row.key] = row;
    }
    final sorted = newest.values.toList();
    // A stable sort keeps existing remote rows first on timestamp ties.
    mergeSort(sorted, compare: (a, b) => b.updated.compareTo(a.updated));
    return sorted.take(maxSyncedVodRows).toList();
  }
}
