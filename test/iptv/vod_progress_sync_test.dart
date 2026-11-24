import 'dart:async';
import 'dart:convert';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/database/app_database.dart';
import 'package:plezy/iptv/iptv_models.dart';
import 'package:plezy/iptv/iptv_source_repository.dart';
import 'package:plezy/iptv/iptv_synced_vod_progress.dart';
import 'package:plezy/iptv/iptv_vod_progress_sync_service.dart';
import 'package:plezy/profiles/profile.dart';
import 'package:plezy/services/plex_auth_service.dart';
import 'package:plezy/services/settings_service.dart';

import '../test_helpers/prefs.dart';

class _Account extends Fake implements PlexAuthService {
  final entries = <String, Object?>{};
  final writes = <String>[];
  final reading = Completer<void>();
  Completer<void>? hold;
  @override
  Future<List<Map<String, dynamic>>> getUserSettings(String token) async {
    if (!reading.isCompleted) reading.complete();
    await hold?.future;
    return [
      for (final entry in entries.entries) {'id': entry.key, 'value': entry.value},
    ];
  }

  @override
  Future<void> putUserSetting(String token, String id, String value) async {
    writes.add(id);
    entries[id] = value;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Account auth;
  late IptvVodProgressSyncService service;
  late IptvSourceRepository repo;
  late AppDatabase db;
  late SettingsService settings;
  final a = Profile.local(id: 'a', displayName: 'Home', createdAt: DateTime(2026));
  final b = Profile.local(id: 'b', displayName: 'Home', createdAt: DateTime(2026));
  setUp(() async {
    resetSharedPreferencesForTest();
    settings = await SettingsService.getInstance();
    await settings.write(SettingsService.iptvVodProgressSyncEnabled, true);
    auth = _Account();
    service = IptvVodProgressSyncService.forTesting(auth);
    db = AppDatabase.forTesting(NativeDatabase.memory());
    repo = IptvSourceRepository(db);
    addTearDown(db.close);
  });
  Future<String> add(Profile owner, {String label = 'Provider', String user = 'Alice', String path = 'Panel'}) =>
      repo.createSource(
        profileId: owner.id,
        displayName: label,
        kind: IptvInputKind.xtream,
        endpoint: 'https://example.org/$path/player_api.php',
        username: user,
        password: 'test',
      );
  Future<void> save(Profile owner, String source, int position) => repo.saveVodProgress(
    owner.id,
    sourceId: source,
    kind: IptvVodKind.movie,
    itemId: 'movie',
    positionMs: position,
    durationMs: 1000,
    finished: false,
  );
  Future<void> sync(Profile owner, {bool Function()? current}) => service.sync(
    profile: owner,
    repository: repo,
    resolveToken: () async => 'account',
    isCurrent: current ?? () => true,
  );

  test('equal labels stay isolated and a renamed Plex Home user syncs across devices', () async {
    Profile home(String device, String name) => Profile.plexHome(
      id: 'plex-home-$device-user',
      displayName: name,
      plexHomeUserUuid: 'user',
      parentConnectionId: device,
      createdAt: DateTime(2026),
    );
    final first = home('desktop', 'Home'), second = home('tv', 'Renamed');
    expect(vodProgressSettingId(first), vodProgressSettingId(second));
    expect(vodProgressSettingId(a), isNot(vodProgressSettingId(b)));
    await save(first, await add(first), 600);
    await sync(first);
    final id = await add(second, label: 'Television lineup');
    await sync(second);
    final position = await repo.vodProgress(second.id, id, kind: IptvVodKind.movie, itemId: 'movie');
    expect(position?.positionMs, 600);
    expect(auth.writes, [vodProgressSettingId(first)]);
  });

  test('unconfigured sources survive an upload and case-sensitive provider identities stay distinct', () async {
    final id = await add(a);
    await save(a, id, 100);
    final local = (await repo.exportVodProgressForSync(a.id)).single;
    final elsewhere = {...local, 'source': 'a' * 64, 'position': 900};
    auth.entries[vodProgressSettingId(a)] = jsonEncode({
      'v': 2,
      'rows': [elsewhere],
    });
    await sync(a);
    final rows = SyncedVodProgress.decode(auth.entries[vodProgressSettingId(a)]);
    expect(rows.map((r) => r.position), unorderedEquals([100, 900]));
    await save(a, await add(a, user: 'alice'), 200);
    await save(a, await add(a, path: 'panel'), 300);
    await sync(a);
    expect(
      SyncedVodProgress.decode(auth.entries[vodProgressSettingId(a)]).map((r) => r.position),
      unorderedEquals([100, 200, 300, 900]),
    );
    final count = auth.writes.length;
    await sync(a);
    expect(auth.writes.length, count);
  });

  test('malformed and future snapshots survive a sync and legacy name-only data is left intact', () async {
    final key = vodProgressSettingId(a);
    await save(a, await add(a), 200);
    for (final raw in [null, '{', '{"v":3,"rows":[]}', '{"v":2,"rows":[{}]}']) {
      auth.entries[key] = raw;
      await sync(a);
      expect(auth.entries[key], raw);
    }
    expect(auth.writes, isEmpty);
    auth.entries.remove(key);
    auth.entries['plezy_iptv_vod_progress_v1'] = 'old';
    await sync(a);
    expect(auth.entries['plezy_iptv_vod_progress_v1'], 'old');
    expect(auth.writes, [key]);
  });

  test('a profile switch queues the new request and stops the old request before merging', () async {
    final id = await add(a);
    await save(a, id, 150);
    final local = (await repo.exportVodProgressForSync(a.id)).single;
    auth.entries[vodProgressSettingId(a)] = jsonEncode({
      'v': 2,
      'rows': [
        {...local, 'position': 550, 'updated': (local['updated'] as int) + 1},
      ],
    });
    await save(b, await add(b), 250);
    auth.hold = Completer<void>();
    var current = true;
    final old = sync(a, current: () => current);
    await auth.reading.future;
    current = false;
    final next = sync(b);
    auth.hold!.complete();
    await Future.wait([old, next]);
    expect((await repo.vodProgress(a.id, id, kind: IptvVodKind.movie, itemId: 'movie'))?.positionMs, 150);
    expect(auth.writes, [vodProgressSettingId(b)]);
  });

  test('opting out during the account read prevents the upload', () async {
    await save(a, await add(a), 200);
    auth.hold = Completer<void>();
    final pending = sync(a);
    await auth.reading.future;
    await settings.write(SettingsService.iptvVodProgressSyncEnabled, false);
    auth.hold!.complete();
    await pending;
    expect(auth.writes, isEmpty);
  });

  test('a cancelled database import rolls back the rows in that transaction', () async {
    final id = await add(a);
    await save(a, id, 100);
    final local = (await repo.exportVodProgressForSync(a.id)).single;
    final rows = [
      for (final item in ['two', 'three']) SyncedVodProgress.fromJson({...local, 'item': item}),
    ];
    var checks = 0;
    await expectLater(repo.mergeVodProgressFromSync(a.id, rows, isCurrent: () => ++checks < 3), throwsStateError);
    expect((await repo.exportVodProgressForSync(a.id)).map((r) => r['item']), ['movie']);
  });
}
