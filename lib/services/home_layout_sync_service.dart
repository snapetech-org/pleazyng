import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/widgets.dart';
import 'package:path_provider/path_provider.dart';

import '../utils/app_logger.dart';
import '../utils/serial_future_queue.dart';
import 'base_shared_preferences_service.dart';
import 'plex_auth_service.dart';
import 'settings_service.dart';

part 'home_layout_synced_pref.dart';

/// Keeps the Home layout (row order, which rows show, Hero and Auto-slide per
/// row, per-library row order) the same on every Plezy signed in to the same
/// Plex account.
///
/// The layout is stored as one hidden shared setting on the plex.tv account,
/// the same mechanism Plex Web uses for its own experience settings. Last
/// writer wins: each push stamps the layout with the time it was changed, and
/// a pull only applies a remote layout that is newer than the one this device
/// last applied or pushed. A device with an unpushed local change skips the
/// pull and pushes instead.
///
/// Only the settings are shared. How a device draws them (TV spotlight vs
/// desktop list) stays per platform.
class HomeLayoutSyncService with WidgetsBindingObserver {
  HomeLayoutSyncService._();

  @visibleForTesting
  HomeLayoutSyncService.forTesting(PlexAuthService auth) : _auth = auth;

  static final HomeLayoutSyncService instance = HomeLayoutSyncService._();

  static const String settingId = 'plezy_home_layout';
  static const String _stateKey = 'home_layout_sync_state';
  static const String _ownerKey = 'home_layout_sync_display_owner';
  static const String _unshared = 'unshared';
  static const String _migratedKey = 'home_layout_sync_enabled_migrated';
  static const Duration _pushDelay = Duration(seconds: 2);
  static const Duration _minPullGap = Duration(seconds: 20);
  static const Duration _periodicPull = Duration(minutes: 1);

  static final List<_SyncedPref<dynamic>> _prefs = [
    _SyncedPref.json(SettingsService.homeSections),
    _SyncedPref.json(SettingsService.homeRowOrder),
    _SyncedPref.json(SettingsService.managedHubHeroOverrides),
    _SyncedPref.json(SettingsService.libraryContinueWatchingHidden),
    _SyncedPref.json(SettingsService.libraryManagedRowOrder),
    _SyncedPref.boolean(SettingsService.continueWatchingOnHome),
    _SyncedPref.boolean(SettingsService.showHeroSection),
    _SyncedPref.boolean(SettingsService.hideWatchedInHubs),
  ];

  @visibleForTesting
  static List<String> debugSyncedPrefTypesForTesting() => [for (final synced in _prefs) synced.runtimeType.toString()];

  /// Whether [local] changed since [savedSnapshot] (the JSON snapshot taken at
  /// the last sync). Keys the saved snapshot doesn't have yet are ignored:
  /// a pref added to the synced list in an app update is not a user change,
  /// and counting it would make every device upload its own layout as the
  /// newest one the first time it runs the update.
  @visibleForTesting
  static bool changedSinceSnapshot(String savedSnapshot, Map<String, Object?> local) {
    if (savedSnapshot.isEmpty) return false;
    final Map<String, dynamic> saved;
    try {
      saved = json.decode(savedSnapshot) as Map<String, dynamic>;
    } catch (_) {
      // best-effort: A comparison failure must leave the local layout eligible for synchronization.
      return true;
    }
    for (final entry in saved.entries) {
      if (!local.containsKey(entry.key)) continue;
      if (json.encode(entry.value) != json.encode(local[entry.key])) return true;
    }
    return false;
  }

  /// Whether the account already holds this device's layout. A key the
  /// account doesn't have yet only matches while this device still has that
  /// pref at its default.
  bool _sameAsRemote(SettingsService settings, Map<String, Object?> local, Map<String, Object?> remote) {
    for (final synced in _prefs) {
      final key = synced.pref.key;
      if (!remote.containsKey(key)) {
        if (!synced.isDefault(settings)) return false;
        continue;
      }
      if (json.encode(remote[key]) != json.encode(local[key])) return false;
    }
    return true;
  }

  /// Human-readable last sync result for the Organizer ("Synced ... at 6:41").
  final ValueNotifier<String> status = ValueNotifier('Not synced yet');

  /// Bumped each time a layout from the account is applied, so an open
  /// Organizer can reload its rows.
  final ValueNotifier<int> appliedRevision = ValueNotifier(0);

  String? _token;
  PlexAuthService? _auth;
  bool _listening = false;
  bool _applying = false;
  String? _owner;
  int _generation = 0;
  bool _initialized = false;
  Map<String, Object?> _unknownPrefs = const {};
  final _operations = SerialFutureQueue();
  bool _current(int generation) => generation == _generation && _owner != null && enabled;

  Timer? _pushTimer;
  Timer? _periodicTimer;
  DateTime? _lastPull;

  static String _clock(DateTime t) {
    final h = t.hour % 12 == 0 ? 12 : t.hour % 12;
    return '$h:${t.minute.toString().padLeft(2, '0')}:${t.second.toString().padLeft(2, '0')} ${t.hour < 12 ? 'AM' : 'PM'}';
  }

  /// Sets [status] and appends one line to `home_layout_sync.log` in the app
  /// support directory, so a sync problem can be diagnosed on any device
  /// (release builds don't keep the app log anywhere readable).
  void _trace(String message, {String? statusText}) {
    final now = DateTime.now();
    if (statusText != null) status.value = '$statusText at ${_clock(now)}';
    unawaited(() async {
      try {
        final file = File(
          '${(await getApplicationSupportDirectory()).path}${Platform.pathSeparator}home_layout_sync.log',
        );
        if (await file.exists() && await file.length() > 256 * 1024) await file.writeAsString('');
        await file.writeAsString('${now.toIso8601String()} $message\n', mode: FileMode.append, flush: true);
      } catch (_) {
        appLogger.d('Could not write the Home layout diagnostic log.');
      }
    }());
  }

  /// Called whenever the active profile's Plex token is known (or gone).
  /// A new token pulls right away; null stops syncing (Jellyfin, signed out).
  /// Whether this device shares its layout ([SettingsService.homeLayoutSyncEnabled]).
  bool get enabled => SettingsService.instance.read(SettingsService.homeLayoutSyncEnabled);

  /// Turn sharing on or off for this device. Turning it on pulls right away:
  /// the account's layout applies if it is newer, otherwise this device's
  /// layout is uploaded.
  Future<void> setEnabled(bool value) async {
    _generation++;
    _initialized = false;
    await SettingsService.instance.write(SettingsService.homeLayoutSyncEnabled, value);
    _pushTimer?.cancel();
    _pushTimer = null;
    if (value) {
      await pull(force: true);
    } else {
      status.value = 'Off on this device';
    }
  }

  /// Devices that synced before the on/off switch existed keep syncing.
  void _migrateEnabled(SettingsService settings) {
    if (settings.readNullableString(_migratedKey) != null) return;
    unawaited(settings.writeString(_migratedKey, '1'));
    if (_readState(settings, _stateKey).appliedAt > 0) {
      unawaited(settings.write(SettingsService.homeLayoutSyncEnabled, true));
    }
  }

  void bind(String? token, {String? accountId, String? userUuid, String? localProfileId}) {
    final settings = SettingsService.instance;
    _migrateEnabled(settings);
    final owner = token == null || token.isEmpty || accountId == null || accountId.isEmpty
        ? (localProfileId == null ? null : 'local_${base64Url.encode(utf8.encode(localProfileId))}')
        : base64Url.encode(utf8.encode(jsonEncode([accountId, userUuid])));
    if (_owner == owner && _token == token) return;
    _generation++;
    _initialized = false;
    _owner = owner;
    _token = owner == null ? null : token;
    _unknownPrefs = const {};
    _lastPull = null;
    _pushTimer?.cancel();
    _pushTimer = null;
    _periodicTimer?.cancel();
    _periodicTimer = null;
    if (owner == null) return;
    if (!enabled) {
      status.value = 'Off on this device';
      final generation = _generation;
      unawaited(
        _operations.run(() async {
          if (enabled || generation != _generation) return;
          final displayed = settings.readNullableString(_ownerKey);
          if (_isDisplayOwner(displayed) && displayed != owner) {
            await settings.writeString('home_layout_local_$displayed', jsonEncode(_snapshot(settings)));
            if (generation == _generation && !enabled) await settings.writeString(_ownerKey, _unshared);
          }
        }),
      );
    }
    _startListening();
    _periodicTimer = Timer.periodic(_periodicPull, (_) => unawaited(pull()));
    unawaited(pull(force: true));
  }

  @visibleForTesting
  Future<void> get settled => _operations.settled;

  @visibleForTesting
  void disposeForTesting() {
    bind(null);
    if (_listening) {
      for (final synced in _prefs) {
        SettingsService.instance.listenableOf(synced.pref).removeListener(_onLocalChange);
      }
      WidgetsBinding.instance.removeObserver(this);
      _listening = false;
    }
  }

  void _startListening() {
    if (_listening) return;
    _listening = true;
    final settings = SettingsService.instance;
    for (final synced in _prefs) {
      settings.listenableOf(synced.pref).addListener(_onLocalChange);
    }
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(pull());
  }

  void _onLocalChange() {
    if (_applying || !_initialized || _token == null || !enabled) return;
    _pushTimer?.cancel();
    _pushTimer = Timer(_pushDelay, () {
      _pushTimer = null;
      unawaited(push());
    });
  }

  Map<String, Object?> _snapshot(SettingsService settings) => {
    for (final synced in _prefs) synced.pref.key: synced.encode(settings),
  };

  ({int appliedAt, String snapshot}) _readState(SettingsService settings, String key) {
    final raw = settings.readNullableString(key);
    if (raw == null) return (appliedAt: 0, snapshot: '');
    try {
      final map = json.decode(raw) as Map<String, dynamic>;
      return (appliedAt: (map['appliedAt'] as num?)?.toInt() ?? 0, snapshot: map['snapshot'] as String? ?? '');
    } catch (_) {
      // best-effort: Reconcile from an empty snapshot when the previous layout state is unreadable.
      return (appliedAt: 0, snapshot: '');
    }
  }

  Future<void> _writeState(SettingsService settings, String owner, int stamp, String snapshot) =>
      settings.writeString('${_stateKey}_$owner', jsonEncode({'appliedAt': stamp, 'snapshot': snapshot}));

  Future<PlexAuthService> _authService() async => _auth ??= await PlexAuthService.create();

  Future<void> _applyLayout(SettingsService settings, Map<String, Object?> values, int generation) async {
    _applying = true;
    try {
      for (final synced in _prefs) {
        if (!_current(generation)) return;
        final value = values.containsKey(synced.pref.key) ? values[synced.pref.key] : synced._defaultJson;
        if (jsonEncode(value) != jsonEncode(synced.encode(settings))) await synced.apply(settings, value);
      }
    } finally {
      _applying = false;
    }
  }

  Future<void> _restore(
    SettingsService settings,
    String owner,
    Map<String, Object?> values,
    int? stamp,
    int generation,
  ) async {
    // Validate and normalize every field before publishing a restore journal.
    // Invalid server data cannot leave half a layout saved to preferences.
    final prefs = {
      for (final synced in _prefs)
        synced.pref.key: synced.normalize(
          values.containsKey(synced.pref.key) ? values[synced.pref.key] : synced._defaultJson,
        ),
    };
    await settings.writeString(_ownerKey, jsonEncode({'owner': owner, 'prefs': prefs, 'stamp': stamp}));
    if (!_current(generation)) return;
    await _finishRestore(settings, owner, prefs, stamp, generation);
  }

  Future<void> _finishRestore(
    SettingsService settings,
    String owner,
    Map<String, Object?> prefs,
    int? stamp,
    int generation,
  ) async {
    if (owner == _owner) {
      await _applyLayout(settings, prefs, generation);
      if (!_current(generation)) return;
    }
    final encoded = jsonEncode(prefs);
    await settings.writeString('home_layout_local_$owner', encoded);
    if (!_current(generation)) return;
    if (stamp != null) await _writeState(settings, owner, stamp, encoded);
    if (!_current(generation) || owner != _owner) return;
    await settings.writeString(_ownerKey, owner);
    if (_current(generation)) appliedRevision.value++;
  }

  Future<void> pull({bool force = false}) {
    final generation = _generation;
    return _operations.run(() => _pull(generation, force));
  }

  bool _isDisplayOwner(String? marker) =>
      marker != null && marker != 'restoring' && marker != _unshared && !marker.startsWith('{');

  Future<void> _pull(int generation, bool force) async {
    if (!_current(generation)) return;
    final owner = _owner!, token = _token;
    final now = DateTime.now();
    if (!force && _lastPull != null && now.difference(_lastPull!) < _minPullGap) return;
    _lastPull = now;
    try {
      final settings = SettingsService.instance;
      var displayed = settings.readNullableString(_ownerKey);
      if (displayed != null && displayed.startsWith('{')) {
        final pending = jsonDecode(displayed) as Map<String, dynamic>;
        final target = pending['owner'] as String;
        await _finishRestore(
          settings,
          target,
          pending['prefs'] as Map<String, dynamic>,
          pending['stamp'] as int?,
          generation,
        );
        if (!_current(generation)) return;
        displayed = target == owner ? owner : 'restoring';
      }
      final state = _readState(settings, '${_stateKey}_$owner');
      final firstDeviceLayout = displayed == null && settings.readNullableString(_stateKey) == null;
      if (displayed != owner && !firstDeviceLayout) {
        // The display owner is durable even if the device restarted directly
        // into this profile before the previous owner's pending push ran.
        if (_isDisplayOwner(displayed)) {
          await settings.writeString('home_layout_local_$displayed', jsonEncode(_snapshot(settings)));
          if (!_current(generation)) return;
        }
        final saved = settings.readNullableString('home_layout_local_$owner') ?? state.snapshot;
        await _restore(
          settings,
          owner,
          saved.isEmpty ? {} : jsonDecode(saved) as Map<String, dynamic>,
          null,
          generation,
        );
        if (!_current(generation)) return;
      } else if (displayed != owner) {
        await settings.writeString(_ownerKey, owner);
        if (!_current(generation)) return;
      }
      if (token == null) {
        _initialized = true;
        return;
      }
      final auth = await _authService();
      if (!_current(generation)) return;
      final entries = await auth.getUserSettings(token);
      if (!_current(generation)) return;
      final entry = entries.singleWhere((e) => e['id'] == settingId, orElse: () => const {});
      final raw = entry['value'];
      final local = _snapshot(settings);
      final dirty = state.snapshot.isNotEmpty && changedSinceSnapshot(state.snapshot, local);
      if (entry.isEmpty) {
        _unknownPrefs = const {};
        _initialized = true;
        if (_prefs.any((p) => !p.isDefault(settings))) {
          await _push(generation, force: true, modifiedAt: dirty || state.appliedAt == 0 ? null : state.appliedAt);
        } else if (state.snapshot.isEmpty) {
          await _writeState(settings, owner, 0, jsonEncode(local));
        }
        return;
      }
      final remote = jsonDecode(raw as String) as Map<String, dynamic>;
      if (remote.containsKey('v') && remote['v'] != 1) throw const FormatException('Unsupported home layout version');
      final stamp = remote['modifiedAt'] as int;
      if (stamp < 0) throw const FormatException('Invalid home layout timestamp');
      final prefs = Map<String, Object?>.from(remote['prefs'] as Map);
      for (final synced in _prefs) {
        final key = synced.pref.key;
        if (prefs.containsKey(key)) prefs[key] = synced.normalize(prefs[key]);
      }
      _unknownPrefs = Map.of(prefs)..removeWhere((key, _) => local.containsKey(key));
      if (_pushTimer != null) return;
      if (state.snapshot.isNotEmpty && (dirty || stamp <= state.appliedAt)) {
        _initialized = true;
        if (!_sameAsRemote(settings, local, prefs)) {
          await _push(generation, force: true, modifiedAt: dirty ? null : state.appliedAt);
        } else {
          // Retire an offline edit another device has independently made. It
          // must not override later server changes once both sides agree.
          await _writeState(settings, owner, stamp > state.appliedAt ? stamp : state.appliedAt, jsonEncode(local));
          if (!_current(generation)) return;
          _trace('up to date', statusText: 'Up to date with your Plex account');
        }
        return;
      }
      final backupKey = 'home_layout_sync_backup_$owner';
      if (state.snapshot.isEmpty && settings.readNullableString(backupKey) == null) {
        await settings.writeString(backupKey, jsonEncode(local));
      }
      if (!_current(generation)) return;
      await _restore(settings, owner, prefs, stamp, generation);
      if (!_current(generation)) return;
      _initialized = true;
      _trace('applied $stamp', statusText: 'Got the latest layout from your Plex account');
    } catch (e) {
      if (!_current(generation)) return;
      _initialized = false;
      appLogger.w('HomeLayoutSync: pull failed', error: e);
      _trace('pull failed: $e', statusText: 'Sync failed ($e)');
    }
  }

  Future<void> push({bool force = false, int? modifiedAt}) {
    final generation = _generation;
    return _operations.run(() => _push(generation, force: force, modifiedAt: modifiedAt));
  }

  Future<void> _push(int generation, {required bool force, int? modifiedAt}) async {
    if (!_current(generation) || !_initialized || _token == null) return;
    final owner = _owner!, token = _token!;
    try {
      final settings = SettingsService.instance;
      if (settings.readNullableString(_ownerKey) != owner) return;
      final snapshot = _snapshot(settings);
      final encoded = jsonEncode(snapshot);
      final state = _readState(settings, '${_stateKey}_$owner');
      if (!force && encoded == state.snapshot) return;
      final stamp = modifiedAt ?? DateTime.now().millisecondsSinceEpoch;
      final auth = await _authService();
      if (!_current(generation)) return;
      await auth.putUserSetting(
        token,
        settingId,
        jsonEncode({
          'v': 1,
          'modifiedAt': stamp,
          'prefs': {..._unknownPrefs, ...snapshot},
        }),
      );
      if (!_current(generation)) return;
      await _writeState(settings, owner, stamp, encoded);
      if (_current(generation)) _trace('pushed $stamp', statusText: 'Saved your layout to your Plex account');
    } catch (e) {
      if (!_current(generation)) return;
      appLogger.w('HomeLayoutSync: push failed', error: e);
      _trace('push failed: $e', statusText: 'Sync failed ($e)');
    }
  }
}
