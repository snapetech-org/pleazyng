import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:plezy/sources/gpodder_client.dart';
import 'package:plezy/sources/podcast_sync.dart';
import 'package:plezy/sources/source_controller.dart';
import 'package:plezy/sources/source_database.dart';
import 'package:plezy/sources/source_models.dart';
import 'package:plezy/sources/source_transport.dart';

import '../test_helpers/io_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SourceController controller;
  late PodcastSync sync;
  late Future<http.Response> Function(http.Request) respond;
  final config = GpodderConfig(
    endpoint: 'https://sync.example',
    username: 'user',
    password: 'example',
    device: 'player',
    progress: false,
  );

  setUp(() async {
    final previous = PathProviderPlatform.instance;
    final directory = await Directory.systemTemp.createTemp('podcast-sync-');
    PathProviderPlatform.instance = FakePathProvider(directory);
    final repository = SourceRepository(
      SourceDatabase(NativeDatabase.memory()),
      protect: (s) async => s,
      reveal: (s) async => s,
    );
    controller = SourceController(repository: repository);
    await controller.ready;
    controller.profileId = 'profile';
    controller.loading = false;
    controller.blocked = false;
    sync = PodcastSync(
      controller,
      onUpdate: () {},
      newTransport: () => SourceTransport(client: MockClient(respond)),
    );
    addTearDown(() async {
      sync.cancel();
      await sync.settled;
      controller.dispose();
      await Future<void>.delayed(Duration.zero);
      PathProviderPlatform.instance = previous;
      await directory.delete(recursive: true);
    });
  });

  test('disconnect stays effective when an earlier login finishes late', () async {
    final started = Completer<void>(), release = Completer<void>();
    respond = (_) async {
      started.complete();
      await release.future;
      return http.Response('', 200);
    };
    final login = sync.configure(config).then<void>((_) {}, onError: (Object _) {});
    await started.future;
    final disconnect = sync.configure(null);
    await Future<void>.delayed(Duration.zero);
    release.complete();
    await Future.wait([login, disconnect]);
    expect(await sync.config(), isNull);
    expect(sync.phase, PodcastSyncPhase.disabled);
  });

  test('a run after cancellation waits for cleanup and processes the new profile', () async {
    final started = Completer<void>(), release = Completer<void>();
    var reads = 0;
    respond = (request) async {
      if (request.method == 'GET' && ++reads == 1) {
        started.complete();
        await release.future;
      }
      return http.Response('{"add":[],"remove":[],"timestamp":1}', 200);
    };
    await sync.configure(config);
    await controller.repository.savePrivatePreference('second', PodcastSync.configKey, config.toJson());
    final first = sync.run();
    await started.future;
    sync.cancel();
    controller.generation++;
    controller.profileId = 'second';
    final second = sync.run();
    release.complete();
    await Future.wait([first, second]);
    expect(reads, 2);
    expect(sync.phase, PodcastSyncPhase.complete);
  });

  test('resubscribing cancels a remote removal deferred during playback', () async {
    const feed = 'https://feed.example/rss';
    const source = FileSource(
      id: 'rss',
      profileId: 'profile',
      kind: FileSourceKind.podcastRss,
      name: 'Podcast',
      endpoint: 'https://feed.example',
    );
    await controller.repository.saveSource(source, {'feedUrl': feed});
    controller.sources = [source];
    var reads = 0;
    respond = (request) async {
      if (request.method == 'POST') return http.Response('{}', 200);
      reads++;
      return http.Response(
        jsonEncode({
          'timestamp': reads,
          'add': [if (reads == 3) feed],
          'remove': [if (reads == 2) feed],
        }),
        200,
      );
    };
    await sync.configure(config);
    await sync.run();
    controller.activeProgressKeys.add('playing');
    await sync.run();
    expect(sync.pendingCount, 1);
    controller.activeProgressKeys.clear();
    await sync.run();
    expect(sync.phase, PodcastSyncPhase.complete);
    expect(sync.pendingCount, 0);
    expect((await controller.repository.sources('profile')).map((s) => s.id), [source.id]);
  });
}
