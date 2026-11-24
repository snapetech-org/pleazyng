import 'package:flutter/foundation.dart';
import 'package:drift/drift.dart';
import 'package:drift/native.dart' show SqliteException;
// Drift wraps SQLite errors from its native background connection in this
// type. Reading its cause avoids matching on error text.
// ignore: experimental_member_use
import 'package:drift/remote.dart' show DriftRemoteException;

import '../../profiles/profile.dart';
import '../../sources/source_database.dart';
import '../base_shared_preferences_service.dart';
import '../credential_vault.dart';
import 'remote_stream_models.dart';

part 'remote_stream_journal_writes.dart';
part 'remote_preparation_claim_store.dart';

enum RemoteStreamStoreFailure { unreadable, claimLost }

class RemotePreparationClaim {
  RemotePreparationClaim(this.profile, this.job, this.owner);
  final String profile, job, owner;
  bool active = true;
}

class RemoteStreamStoreException implements Exception {
  const RemoteStreamStoreException(this.reason);
  final RemoteStreamStoreFailure reason;
}

/// Transactional encrypted state shared by UI and headless worker engines.
class RemoteStreamStore {
  static Future<SourceDatabase>? _opening;
  @visibleForTesting
  static void useDatabaseForTesting(SourceDatabase? database) {
    _opening = database == null ? null : Future.value(database);
  }

  static Future<SourceDatabase> get _database => _opening ??= SourceDatabase.open();
  static Future<RemoteStreamSnapshot> _decode(String raw) async {
    final clear = await CredentialVault.reveal(raw);
    if (clear == null) throw const RemoteStreamStoreException(RemoteStreamStoreFailure.unreadable);
    try {
      return RemoteStreamSnapshot.decode(clear);
    } on Object {
      throw const RemoteStreamStoreException(RemoteStreamStoreFailure.unreadable);
    }
  }

  static Future<RemoteStreamSnapshot> load(String profile) async {
    final db = await _database;
    final row = await db
        .customSelect('SELECT payload FROM remote_stream_state WHERE profile=?', variables: [Variable(profile)])
        .getSingleOrNull();
    if (row != null) return _decode(row.read<String>('payload'));
    return update(profile, (snapshot) => snapshot);
  }

  static Future<void> save(String profile, RemoteStreamSnapshot snapshot) async {
    await update(profile, (_) => snapshot);
  }

  static Future<RemoteStreamSnapshot> update(
    String profile,
    RemoteStreamSnapshot Function(RemoteStreamSnapshot) change, {
    RemotePreparationClaim? claim,
  }) async {
    final db = await _database;
    final prefs = await BaseSharedPreferencesService.sharedCache();
    final legacy = prefs.getString(profileScopedPrefsKey(profile, 'remote_streaming_v1'));
    final seed = legacy ?? await CredentialVault.protect(const RemoteStreamSnapshot().encode());
    final next = await db._writeJournal(() async {
      await db._requireLiveProfile(profile);
      await db.customStatement('INSERT OR IGNORE INTO remote_stream_state VALUES (?,?)', [profile, seed]);
      final row = await db
          .customSelect('SELECT payload FROM remote_stream_state WHERE profile=?', variables: [Variable(profile)])
          .getSingle();
      final current = await _decode(row.read<String>('payload'));
      final expires = claim == null ? null : await db._preparationClaimExpiry(profile, claim);
      final next = change(current);
      final payload = await CredentialVault.protect(next.encode());
      if (claim != null) _requireActiveClaim(claim, expires);
      await db.customStatement('UPDATE remote_stream_state SET payload=? WHERE profile=?', [payload, profile]);
      return next;
    });
    if (legacy != null) await prefs.remove(profileScopedPrefsKey(profile, 'remote_streaming_v1'));
    return next;
  }

  static Future<void> clearUnreadable(String profile, {required bool Function() isCurrent}) async {
    final db = await _database;
    final prefs = await BaseSharedPreferencesService.sharedCache();
    final legacyKey = profileScopedPrefsKey(profile, 'remote_streaming_v1');
    final legacy = prefs.getString(legacyKey);
    final usable = await db._writeJournal(() => db._repairSettings(profile, legacy, isCurrent));
    if (usable && legacy != null) await prefs.remove(legacyKey);
  }

  static Future<List<String>> profiles() async =>
      (await (await _database).customSelect('SELECT profile FROM remote_stream_state').get())
          .map((row) => row.read<String>('profile'))
          .toList();

  /// Keep the idle decision and OS removal ordered before a producer's next
  /// journal commit. That producer then re-enqueues after its commit; cleanup
  /// cannot remove a task installed for a newly committed preparation.
  static Future<void> cancelRecoveryWhenIdle(Future<void> Function() cancel) async {
    final db = await _database;
    await db._writeJournal(() async {
      for (final row in await db.customSelect('SELECT payload FROM remote_stream_state').get()) {
        final snapshot = await _decode(row.read<String>('payload'));
        if (snapshot.preparations.any(
          (job) =>
              job.canRecoverAutomatically &&
              snapshot.accounts.any((account) => account.id == job.accountId && account.service == job.service),
        )) {
          return;
        }
      }
      await cancel();
    });
  }

  static Future<bool> claim(String profile, String job, String owner) async =>
      (await _database)._claimPreparation(profile, job, owner);

  static Future<void> release(String profile, String job, String owner) async =>
      (await _database)._releasePreparation(profile, job, owner);

  static Future<bool> renewClaim(String profile, String job, String owner) async =>
      (await _database)._renewPreparation(profile, job, owner);
}
