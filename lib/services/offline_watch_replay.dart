part of 'offline_watch_sync_service.dart';

extension _PendingWatchReplay on OfflineWatchSyncService {
  Future<void> _pushPendingItems() async {
    if (_backgroundSyncStopped || isDisposed) return;
    _isSyncing = true;
    safeNotifyListeners();

    try {
      final profileId = _activeProfileId, generation = _profileGeneration;
      await _adoptLegacyWatchActionsForActiveProfile();
      if (_backgroundSyncStopped || isDisposed || generation != _profileGeneration) return;
      if (profileId == null || profileId.isEmpty) {
        // No active profile: dropping the filter would replay EVERY
        // profile's queued actions through whatever clients happen to be
        // bound — the wrong user's account. Actions stay queued.
        appLogger.d('No active profile — deferring pending watch sync');
        return;
      }
      final pendingActions = await _database.getPendingWatchActions(profileId: profileId);

      if (pendingActions.isEmpty) {
        appLogger.d('No pending watch actions to sync');
        return;
      }

      appLogger.i('Syncing ${pendingActions.length} pending watch actions');

      final blocked = <(String, String?, String?)>{};
      for (final action in pendingActions) {
        final lineage = (action.globalKey, action.clientScopeId, action.accountScope);
        if (blocked.contains(lineage)) continue;
        if (_backgroundSyncStopped) return;
        if (isDisposed || generation != _profileGeneration) {
          // A profile switch mid-loop rebinds server clients to the NEW
          // user's tokens under the same server ids — replaying the rest
          // would write this profile's watch history to another account.
          // Remaining actions stay queued for the next sync.
          appLogger.i('Active profile changed mid-sync — requeueing remaining watch actions');
          return;
        }
        if (action.syncAttempts >= OfflineWatchSyncService.maxSyncAttempts) {
          blocked.add(lineage);
          appLogger.w(
            'Skipping action ${action.id} - exceeded retry limit '
            '(${action.syncAttempts} attempts). Last error: ${action.lastError}',
          );
          continue;
        }

        final synced = await serializeWatchWrite(action.globalKey, () async {
          if (_backgroundSyncStopped || isDisposed || generation != _profileGeneration) return false;
          if (!await _database.watchActionIsCurrent(action.id, action.updatedAt)) return true;
          return _withOnlineClientForAction(action, (client, clientScopeId) async {
            try {
              final result = await _syncAction(client, action, clientScopeId: clientScopeId, generation: generation);
              if (!result.persisted) {
                // The write did not land — MediaBrowser drops a stop for a
                // session its Started never opened. Leave the row queued and
                // count the attempt so a later pass retries it.
                blocked.add(lineage);
                appLogger.d('Action ${action.id} did not persist; keeping it queued');
                await _database.updateSyncAttemptIfUnchanged(action.id, action.updatedAt, 'write did not persist');
                return;
              }
              final deleted = await _database.deleteWatchActionIfUnchanged(result.rowId, result.revision);
              if (!deleted) {
                appLogger.d('Synced action ${action.id} revision ${action.updatedAt}; a newer revision remains queued');
                return;
              }
              WatchPatchPromotionNotifier().promote(
                WatchPatchId.offlineAction(profileId: result.profileId, rowId: result.rowId, revision: result.revision),
              );
              appLogger.d('Successfully synced action ${action.id}: ${action.actionType} for ${action.ratingKey}');
            } on _RetiredWatchOwner {
              return;
            } catch (e) {
              blocked.add(lineage);
              appLogger.w('Failed to sync action ${action.id}: $e');
              await _database.updateSyncAttemptIfUnchanged(action.id, action.updatedAt, e.toString());
            }
          });
        });
        if (!synced) {
          blocked.add(lineage);
          appLogger.d('Keeping action ${action.id} queued until server is available');
          continue;
        }
      }
    } finally {
      _isSyncing = false;
      safeNotifyListeners();
    }
  }
}
