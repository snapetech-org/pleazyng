import 'dart:io';
import 'dart:ui';

import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

import 'remote_stream_store.dart';
import 'remote_streaming_provider.dart';
import 'preparation_scheduler.dart';

part 'remote_preparation_entrypoints.dart';

Future<bool> runRemotePreparationWorker() async {
  final worker = RemoteStreamingProvider(backgroundWorker: true);
  try {
    final deadline = DateTime.now().add(const Duration(minutes: 6));
    var success = true;
    for (final profile in await RemoteStreamStore.profiles()) {
      if (DateTime.now().isAfter(deadline)) return false;
      success = await _runProfile(worker, profile, deadline) && success;
    }
    if (success) await RemoteStreamStore.cancelRecoveryWhenIdle(PreparationScheduler.cancel);
    return success;
  } on Object {
    return false;
  } finally {
    worker.dispose();
  }
}

Future<bool> _runProfile(RemoteStreamingProvider worker, String profile, DateTime deadline) async {
  try {
    await worker.runBackgroundProfile(profile, deadline);
    return true;
  } on Object {
    // Keep the worker scheduled for the failed profile and continue with the
    // remaining journals.
    return false;
  }
}
