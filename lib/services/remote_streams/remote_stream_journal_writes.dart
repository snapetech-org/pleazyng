part of 'remote_stream_store.dart';

extension _RemoteStreamJournalWrites on SourceDatabase {
  Future<T> _writeJournal<T>(Future<T> Function() write) async {
    const sqliteBusy = 5;
    const delays = [Duration(milliseconds: 25), Duration(milliseconds: 100), Duration(milliseconds: 250)];
    final elapsed = Stopwatch()..start();
    for (var attempt = 0; ; attempt++) {
      var started = false;
      try {
        return await transaction(() {
          started = true;
          return write();
        });
      } on Object catch (error) {
        final cause = error is DriftRemoteException ? error.remoteCause : error;
        // BEGIN IMMEDIATE can lose the writer lock to another engine. Once
        // inside the transaction, the update or an OS callback may have run.
        if (started ||
            cause is! SqliteException ||
            cause.resultCode != sqliteBusy ||
            attempt == delays.length ||
            elapsed.elapsed > const Duration(seconds: 12)) {
          rethrow;
        }
        await Future<void>.delayed(delays[attempt]);
      }
    }
  }

  Future<void> _requireLiveProfile(String profile) async {
    final removed = await customSelect(
      'SELECT 1 FROM remote_deleted_profiles WHERE profile=?',
      variables: [Variable(profile)],
    ).getSingleOrNull();
    if (removed != null) throw StateError('Profile was removed');
  }

  /// Rechecks the repair request with the writer lock held. A foreground
  /// load or worker may have already made the settings readable again.
  Future<bool> _repairSettings(String profile, String? legacy, bool Function() isCurrent) async {
    if (!isCurrent()) return false;
    await _requireLiveProfile(profile);
    final row = await customSelect(
      'SELECT payload FROM remote_stream_state WHERE profile=?',
      variables: [Variable(profile)],
    ).getSingleOrNull();
    final recovered = await _readableSnapshot(row?.read<String>('payload') ?? legacy);
    if (!isCurrent()) return false;
    if (row != null && recovered != null) return true;

    final payload = await CredentialVault.protect((recovered ?? const RemoteStreamSnapshot()).encode());
    if (!isCurrent()) return false;
    if (recovered == null) {
      await customStatement('DELETE FROM remote_preparation_claims WHERE profile=?', [profile]);
    }
    await customStatement(
      'INSERT INTO remote_stream_state VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET payload=excluded.payload',
      [profile, payload],
    );
    return true;
  }
}

Future<RemoteStreamSnapshot?> _readableSnapshot(String? raw) async {
  if (raw == null) return null;
  try {
    return await RemoteStreamStore._decode(raw);
  } on RemoteStreamStoreException catch (error) {
    if (error.reason != RemoteStreamStoreFailure.unreadable) rethrow;
    return null;
  }
}
