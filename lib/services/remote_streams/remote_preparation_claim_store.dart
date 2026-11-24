part of 'remote_stream_store.dart';

const _claimDuration = Duration(minutes: 10);

void _requireActiveClaim(RemotePreparationClaim claim, int? expires) {
  if (!claim.active || expires == null || expires <= DateTime.now().millisecondsSinceEpoch) {
    claim.active = false;
    throw const RemoteStreamStoreException(RemoteStreamStoreFailure.claimLost);
  }
}

extension _RemotePreparationClaimStore on SourceDatabase {
  Future<int> _preparationClaimExpiry(String profile, RemotePreparationClaim claim) async {
    final row = await customSelect(
      'SELECT expires FROM remote_preparation_claims WHERE profile=? AND job=? AND owner=?',
      variables: [Variable(profile), Variable(claim.job), Variable(claim.owner)],
    ).getSingleOrNull();
    final expires = claim.profile == profile ? row?.read<int>('expires') : null;
    _requireActiveClaim(claim, expires);
    return expires!;
  }

  Future<bool> _claimPreparation(String profile, String job, String owner) => _writeJournal(() async {
    final now = DateTime.now().millisecondsSinceEpoch;
    await customStatement(
      'INSERT INTO remote_preparation_claims SELECT ?,?,?,? WHERE NOT EXISTS '
      '(SELECT 1 FROM remote_deleted_profiles WHERE profile=?) '
      'ON CONFLICT(profile,job) DO UPDATE SET owner=excluded.owner,expires=excluded.expires '
      'WHERE remote_preparation_claims.expires<=? OR remote_preparation_claims.owner=?',
      [profile, job, owner, now + _claimDuration.inMilliseconds, profile, now, owner],
    );
    final row = await customSelect(
      'SELECT owner FROM remote_preparation_claims WHERE profile=? AND job=?',
      variables: [Variable(profile), Variable(job)],
    ).getSingleOrNull();
    return row?.read<String>('owner') == owner;
  });

  Future<void> _releasePreparation(String profile, String job, String owner) => _writeJournal(
    () => customStatement('DELETE FROM remote_preparation_claims WHERE profile=? AND job=? AND owner=?', [
      profile,
      job,
      owner,
    ]),
  );

  Future<bool> _renewPreparation(String profile, String job, String owner) => _writeJournal(() async {
    // Check time after acquiring the writer lock. Waiting for another engine
    // can take longer than the remaining life of this claim.
    final now = DateTime.now().millisecondsSinceEpoch;
    final changed = await customUpdate(
      'UPDATE remote_preparation_claims SET expires=? WHERE profile=? AND job=? AND owner=? AND expires>?',
      variables: [
        Variable(now + _claimDuration.inMilliseconds),
        Variable(profile),
        Variable(job),
        Variable(owner),
        Variable(now),
      ],
    );
    return changed == 1;
  });
}
