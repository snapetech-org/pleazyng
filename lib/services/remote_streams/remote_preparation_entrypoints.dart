part of 'remote_preparation_worker.dart';

@pragma('vm:entry-point')
Future<void> remotePreparationEntrypoint() async {
  WidgetsFlutterBinding.ensureInitialized();
  DartPluginRegistrant.ensureInitialized();
  final success = await runRemotePreparationWorker();
  await const MethodChannel('org.plezy/preparation.worker').invokeMethod<void>('complete', success);
}

Future<void> runDesktopPreparationWorker() async {
  final success = await runRemotePreparationWorker();
  exit(success ? 0 : 1);
}
