import 'dart:convert';
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/connection/connection.dart';
import 'package:plezy/connection/connection_registry.dart';
import 'package:plezy/profiles/active_plex_token.dart';
import 'package:plezy/profiles/profile.dart';
import 'package:plezy/profiles/profile_connection.dart';
import 'package:plezy/profiles/profile_connection_registry.dart';
import 'package:plezy/profiles/profile_plex_settings_account.dart';
import 'package:plezy/services/home_layout_sync_service.dart';

import 'package:plezy/services/settings_service.dart';
import 'package:plezy/services/plex_auth_service.dart';

import '../test_helpers/prefs.dart';

class _PlexConnections extends Fake implements ConnectionRegistry {
  _PlexConnections(this.accounts);
  final Map<String, PlexAccountConnection> accounts;
  @override
  Future<PlexAccountConnection?> getPlexAccount(String connectionId) async => accounts[connectionId];
}

class _LocalLinks extends Fake implements ProfileConnectionRegistry {
  _LocalLinks(this.rows);
  List<ProfileConnection> rows;
  @override
  Future<List<ProfileConnection>> listForProfile(String profileId) async =>
      rows.where((row) => row.profileId == profileId).toList();
}

class _Accounts implements PlexAuthService {
  final settings = <String, (int, bool)>{'A': (300, false), 'B': (200, true)};
  final writes = <String>[];
  final extraPrefs = <String, Object?>{};
  Map<String, dynamic>? lastPayload;
  int version = 1;
  final readStarted = Completer<void>();
  Completer<void>? delay;
  @override
  Future<List<Map<String, dynamic>>> getUserSettings(String token) async {
    if (!readStarted.isCompleted) readStarted.complete();
    if (token == 'A') await delay?.future;
    final layout = settings[token];
    return layout == null
        ? []
        : [
            {
              'id': HomeLayoutSyncService.settingId,
              'value': jsonEncode({
                'v': version,
                'modifiedAt': layout.$1,
                'prefs': {'show_hero_section': layout.$2, ...extraPrefs},
              }),
            },
          ];
  }

  @override
  Future<void> putUserSetting(String token, String id, String value) async {
    final decoded = jsonDecode(value) as Map<String, dynamic>;
    lastPayload = decoded;
    writes.add(token);
    settings[token] = (decoded['modifiedAt'] as int, decoded['prefs']['show_hero_section'] as bool);
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  test('a local profile borrowing a named Home user waits for its own switched token', () async {
    final profile = Profile.local(id: 'p', displayName: 'Local', createdAt: DateTime(2026));
    final owner = PlexAccountConnection(
      id: 'plex',
      accountToken: 'owner-token',
      clientIdentifier: 'client',
      accountLabel: 'Owner',
      createdAt: DateTime(2026),
    );
    final alternate = owner.copyWith(id: 'alternate', accountToken: 'alternate-token');
    final connections = _PlexConnections({owner.id: owner, alternate.id: alternate});
    final named = ProfileConnection(profileId: profile.id, connectionId: owner.id, userIdentifier: 'child');
    final other = ProfileConnection(profileId: profile.id, connectionId: alternate.id, userIdentifier: '');
    final links = _LocalLinks([named, other]);
    Future<ActivePlexToken?> resolve() =>
        resolveProfilePlexSettingsAccount(profile: profile, connections: connections, profileConnections: links);

    expect(await resolve(), isNull);
    links.rows = [named.copyWith(userToken: ''), other];
    expect(await resolve(), isNull);
    links.rows = [named.copyWith(userToken: 'child-token'), other];
    final child = await resolve();
    expect(child?.token, 'child-token');
    expect(child?.identity.userUuid, 'child');
    links.rows = [named.copyWith(userIdentifier: '')];
    final directOwner = await resolve();
    expect(directOwner?.token, 'owner-token');
    expect(directOwner?.identity.userUuid, isNull);
  });

  group('account switching', () {
    late SettingsService settings;
    late _Accounts accounts;
    late HomeLayoutSyncService sync;
    setUp(() async {
      resetSharedPreferencesForTest();
      settings = await SettingsService.getInstance();
      await settings.write(SettingsService.homeLayoutSyncEnabled, true);
      accounts = _Accounts();
      sync = HomeLayoutSyncService.forTesting(accounts);
      addTearDown(() async {
        sync.disposeForTesting();
        await sync.settled;
      });
    });
    test('a future schema blocks pending writes until a compatible pull succeeds', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      accounts.version = 2;
      accounts.settings['A'] = (400, true);
      await sync.pull(force: true);
      expect(settings.read(SettingsService.showHeroSection), false);
      await sync.push(force: true);
      expect(accounts.writes, isEmpty);
      accounts.version = 1;
      await sync.pull(force: true);
      expect(settings.read(SettingsService.showHeroSection), true);
      await sync.push(force: true);
      expect(accounts.writes, ['A']);
    });

    test('a local edit preserves settings from newer clients using the same schema', () async {
      accounts.extraPrefs['future_row'] = ['kids'];
      sync.bind('A', accountId: 'one');
      await sync.settled;
      await settings.write(SettingsService.showHeroSection, true);
      await sync.push();
      expect(accounts.lastPayload!['prefs']['future_row'], ['kids']);
      expect(accounts.settings['A']!.$2, true);
    });

    test('an older revision belongs to the incoming account and an empty account starts clean', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), false);
      sync.bind('B', accountId: 'two');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), true);
      expect(accounts.settings['B'], (200, true));
      sync.bind('A', accountId: 'one');
      await sync.settled;
      sync.bind('C', accountId: 'three');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), SettingsService.showHeroSection.defaultValue);
      expect(accounts.writes, isEmpty);
    });
    test('a credential change while an old read is in flight discards that response', () async {
      accounts.delay = Completer<void>();
      sync.bind('A', accountId: 'one', userUuid: 'parent');
      await accounts.readStarted.future;
      sync.bind('B', accountId: 'one', userUuid: 'child');
      accounts.delay!.complete();
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), true);
      expect(accounts.writes, isEmpty);
    });
    test('cold start into another profile preserves outgoing offline edits', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      await settings.write(SettingsService.showHeroSection, true);
      sync.disposeForTesting();
      final restarted = HomeLayoutSyncService.forTesting(accounts);
      addTearDown(restarted.disposeForTesting);
      restarted.bind('B', accountId: 'two');
      await restarted.settled;
      restarted.bind('A', accountId: 'one');
      await restarted.settled;
      expect(accounts.writes, ['A']);
      expect(accounts.settings['A']!.$2, true);
    });

    test('offline changes on a non-Plex profile remain in its own layout', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      sync.bind(null, localProfileId: 'local');
      await sync.settled;
      await settings.write(SettingsService.showHeroSection, true);
      sync.bind('A', accountId: 'one');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), false);
      expect(accounts.writes, isEmpty);
      sync.bind(null, localProfileId: 'local');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), true);
    });

    test('an interrupted restore completes its exact target instead of pushing a stale snapshot', () async {
      final owner = base64Url.encode(utf8.encode(jsonEncode(['one', null])));
      await settings.writeString('home_layout_local_$owner', jsonEncode({'show_hero_section': true}));
      await settings.writeString(
        'home_layout_sync_display_owner',
        jsonEncode({
          'owner': owner,
          'prefs': {'show_hero_section': false},
          'stamp': 300,
        }),
      );
      sync.bind('A', accountId: 'one');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), false);
      expect(accounts.writes, isEmpty);
    });

    test('an offline edit that agrees with another device does not override a later account update', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      await sync.setEnabled(false);
      await settings.write(SettingsService.showHeroSection, true);
      accounts.settings['A'] = (400, true);
      await sync.setEnabled(true);
      accounts.settings['A'] = (500, false);
      await sync.pull(force: true);
      expect(settings.read(SettingsService.showHeroSection), false);
      expect(accounts.writes, isEmpty);
    });
    test('switching with sharing disabled does not write the next profile’s edits back to the first', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      await sync.setEnabled(false);
      sync.bind('B', accountId: 'two');
      await sync.settled;
      await settings.write(SettingsService.showHeroSection, true);
      await sync.setEnabled(true);
      sync.bind('A', accountId: 'one');
      await sync.settled;
      expect(settings.read(SettingsService.showHeroSection), false);
      expect(accounts.writes, isEmpty);
    });

    test('a pending edit follows its own account across a switch', () async {
      sync.bind('A', accountId: 'one');
      await sync.settled;
      await settings.write(SettingsService.showHeroSection, true);
      sync.bind('B', accountId: 'two');
      await sync.settled;
      sync.bind('A', accountId: 'one');
      await sync.settled;
      expect(accounts.writes, ['A']);
      expect(accounts.settings['A']!.$2, true);
    });
  });
  test('synced prefs keep their concrete types (no erased JsonPref.encode)', () {
    expect(HomeLayoutSyncService.debugSyncedPrefTypesForTesting(), everyElement(isNot(contains('dynamic'))));
  });

  group('changedSinceSnapshot', () {
    final saved = jsonEncode({
      'home_row_order': ['a', 'b'],
      'show_hero_section': true,
    });

    test('same values are not a change', () {
      expect(
        HomeLayoutSyncService.changedSinceSnapshot(saved, {
          'home_row_order': ['a', 'b'],
          'show_hero_section': true,
        }),
        isFalse,
      );
    });

    test('an edited value is a change', () {
      expect(
        HomeLayoutSyncService.changedSinceSnapshot(saved, {
          'home_row_order': ['b', 'a'],
          'show_hero_section': true,
        }),
        isTrue,
      );
    });

    test('a pref newly added to the synced list is not a change', () {
      expect(
        HomeLayoutSyncService.changedSinceSnapshot(saved, {
          'home_row_order': ['a', 'b'],
          'show_hero_section': true,
          'hide_watched_in_hubs': false,
        }),
        isFalse,
      );
    });

    test('no saved snapshot is not a change', () {
      expect(HomeLayoutSyncService.changedSinceSnapshot('', {'home_row_order': <String>[]}), isFalse);
    });
  });
}
