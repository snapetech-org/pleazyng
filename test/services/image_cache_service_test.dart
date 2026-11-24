import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:cached_network_image_ce/cached_network_image.dart' show FileInfo;
// The IO implementation exposes the directory provider for the legacy cache fixture.
// ignore: implementation_imports
import 'package:cached_network_image_ce/src/cache/default_cache_manager.dart' as ce_cache;
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:path/path.dart' as path;
import 'package:path_provider/path_provider.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:path_provider_webos/path_provider_webos.dart';
import 'package:plezy/services/image_cache_service.dart';
import 'package:plezy/utils/app_cache_directory.dart';
import 'package:plezy/utils/platform_detector.dart';

/// Pins the artwork request limiter's permit lifecycle. CE's cache manager
/// abandons non-200/202 responses without listening to the body, so the
/// wrapper must release those slots itself or the limiter wedges after a
/// handful of stale-thumb 404s (#1473).
void main() {
  const timeout = Duration(seconds: 5);

  test('webOS uses its supported app-scoped temporary cache directory', () async {
    if (!PlatformDetector.isWebOSBuild()) return;

    final root = await Directory.systemTemp.createTemp('plezy-webos-cache-test-');
    final previousProvider = PathProviderPlatform.instance;
    addTearDown(() async {
      PathProviderPlatform.instance = previousProvider;
      if (await root.exists()) await root.delete(recursive: true);
    });

    PathProviderPlatform.instance = PathProviderWebOS.private(
      environment: {'FLUTTER_HOME': root.path, 'FLUTTER_APP_ID': 'org.snapetech.plezyng'},
    );

    final directory = await getPlezyAppCacheDirectory();

    expect(directory.path, path.join(root.path, '.cache', 'org.snapetech.plezyng'));
    expect(await directory.exists(), isTrue);
    await expectLater(getApplicationCacheDirectory(), throwsA(isA<UnimplementedError>()));
  });

  http.Request get(String path) => http.Request('GET', Uri.parse('https://example.invalid$path'));

  MockClient mixedClient() => MockClient((request) async {
    if (request.url.path.contains('missing')) {
      return http.Response('not found', 404, headers: {'x-marker': 'err'});
    }
    return http.Response('poster-bytes', 200);
  });

  test('error burst does not wedge the limiter (#1473)', () async {
    final client = createArtworkHttpClientForTest(mixedClient(), maxConcurrent: 2);

    // More failures than permits, bodies deliberately never read — mirrors
    // CE throwing on the status without listening to the stream.
    for (var i = 0; i < 5; i++) {
      final response = await client.send(get('/missing/$i')).timeout(timeout);
      expect(response.statusCode, 404);
      expect(response.headers['x-marker'], 'err');
    }

    final ok = await client.send(get('/poster')).timeout(timeout);
    expect(ok.statusCode, 200);
    expect(await ok.stream.bytesToString().timeout(timeout), 'poster-bytes');
  });

  test('successful downloads still hold a slot until the body is drained', () async {
    final client = createArtworkHttpClientForTest(mixedClient(), maxConcurrent: 1);

    final first = await client.send(get('/poster')).timeout(timeout);
    var secondDone = false;
    final second = client.send(get('/poster')).then((response) async {
      secondDone = true;
      await response.stream.drain<void>();
    });

    await pumpEventQueue();
    expect(secondDone, isFalse, reason: 'slot must stay held while the first body is unread');

    await first.stream.drain<void>();
    await second.timeout(timeout);
    expect(secondDone, isTrue);
  });

  test('an unclaimed successful body cannot permanently leak a permit', () async {
    final client = createArtworkHttpClientForTest(
      mixedClient(),
      maxConcurrent: 1,
      unclaimedResponseTimeout: const Duration(milliseconds: 20),
    );

    // Mirrors an image widget being disposed after headers arrive but before
    // the cache manager starts listening to the response body.
    await client.send(get('/abandoned')).timeout(timeout);

    final next = await client.send(get('/poster')).timeout(timeout);
    expect(await next.stream.bytesToString().timeout(timeout), 'poster-bytes');
  });

  test('an unclaimed body is cancelled so the transport can reclaim its connection', () async {
    final cancelled = Completer<void>();
    final body = StreamController<List<int>>(onCancel: () => cancelled.complete());
    final client = createArtworkHttpClientForTest(
      MockClient.streaming((request, _) async => http.StreamedResponse(body.stream, 200)),
      maxConcurrent: 1,
      unclaimedResponseTimeout: const Duration(milliseconds: 20),
    );

    await client.send(get('/abandoned')).timeout(timeout);
    await cancelled.future.timeout(timeout);
    await body.close();
  });

  test('error bodies are cancelled so the transport can reclaim the connection', () async {
    final listened = Completer<void>();
    final body = StreamController<List<int>>(onListen: () => listened.complete());
    unawaited(body.close());
    final client = createArtworkHttpClientForTest(
      MockClient.streaming((request, _) async => http.StreamedResponse(body.stream, 404)),
      maxConcurrent: 2,
    );

    final response = await client.send(get('/missing')).timeout(timeout);
    expect(response.statusCode, 404);
    await listened.future.timeout(timeout);
  });

  test('error response keeps its permit until cancellation finishes', () async {
    final cancellationStarted = Completer<void>();
    final allowCancellation = Completer<void>();
    final body = StreamController<List<int>>(
      onCancel: () {
        cancellationStarted.complete();
        return allowCancellation.future;
      },
    );
    var requests = 0;
    final client = createArtworkHttpClientForTest(
      MockClient.streaming((_, _) async {
        requests++;
        return requests == 1 ? http.StreamedResponse(body.stream, 503) : http.StreamedResponse(Stream.value([1]), 200);
      }),
      maxConcurrent: 1,
    );
    final failed = client.send(get('/missing'));
    await cancellationStarted.future;
    final next = client.send(get('/poster'));
    await Future<void>.delayed(Duration.zero);
    expect(requests, 1);
    allowCancellation.complete();
    expect((await failed).statusCode, 503);
    await (await next).stream.drain<void>();
    expect(requests, 2);
    await body.close();
  });

  test('a throwing send releases its slot', () async {
    var failures = 0;
    final client = createArtworkHttpClientForTest(
      MockClient((request) async {
        if (failures < 2) {
          failures++;
          throw http.ClientException('boom');
        }
        return http.Response('poster-bytes', 200);
      }),
      maxConcurrent: 1,
    );

    for (var i = 0; i < 2; i++) {
      await expectLater(client.send(get('/poster')).timeout(timeout), throwsA(isA<http.ClientException>()));
    }

    final ok = await client.send(get('/poster')).timeout(timeout);
    expect(ok.statusCode, 200);
  });

  test('a response whose headers never arrive releases its slot', () async {
    final stalled = Completer<http.StreamedResponse>();
    final client = createArtworkHttpClientForTest(
      MockClient.streaming((request, _) async {
        if (request.url.path == '/stalled') return stalled.future;
        return http.StreamedResponse(Stream.value('poster-bytes'.codeUnits), 200);
      }),
      maxConcurrent: 1,
      stallTimeout: const Duration(milliseconds: 20),
    );

    await expectLater(client.send(get('/stalled')).timeout(timeout), throwsA(isA<TimeoutException>()));

    final ok = await client.send(get('/poster')).timeout(timeout);
    expect(await ok.stream.bytesToString().timeout(timeout), 'poster-bytes');

    // A late answer from a transport that ignored the abort is discarded.
    final lateBodyCancelled = Completer<void>();
    final lateBody = StreamController<List<int>>(onCancel: () => lateBodyCancelled.complete());
    stalled.complete(http.StreamedResponse(lateBody.stream, 200));
    await lateBodyCancelled.future.timeout(timeout);
    await lateBody.close();
  });

  test('a body that stops mid-transfer releases its slot', () async {
    final body = StreamController<List<int>>();
    final client = createArtworkHttpClientForTest(
      MockClient.streaming((request, _) async {
        if (request.url.path == '/stalled') return http.StreamedResponse(body.stream, 200);
        return http.StreamedResponse(Stream.value('poster-bytes'.codeUnits), 200);
      }),
      maxConcurrent: 1,
      stallTimeout: const Duration(milliseconds: 20),
    );

    final stalled = await client.send(get('/stalled')).timeout(timeout);
    body.add('partial'.codeUnits);
    await expectLater(stalled.stream.drain<void>().timeout(timeout), throwsA(isA<TimeoutException>()));

    final ok = await client.send(get('/poster')).timeout(timeout);
    expect(await ok.stream.bytesToString().timeout(timeout), 'poster-bytes');
    await body.close();
  });

  test('cancelling a successful body releases its slot', () async {
    final client = createArtworkHttpClientForTest(mixedClient(), maxConcurrent: 1);

    final first = await client.send(get('/poster')).timeout(timeout);
    final subscription = first.stream.listen((_) {});
    await subscription.cancel();

    final ok = await client.send(get('/poster')).timeout(timeout);
    expect(ok.statusCode, 200);
    await ok.stream.drain<void>();
  });

  group('artwork credentials', () {
    const token = 'secret-token';
    const plexUrl =
        'https://srv.example:32400/photo/:/transcode?width=200&minSize=1'
        '&url=%2Flibrary%2Fmetadata%2F1%2Fthumb%2F99%3FX-Plex-Token%3D$token&X-Plex-Token=$token';

    test('the same poster path keeps each account’s response separate across disk cache hits', () async {
      final dir = await Directory.systemTemp.createTemp('artwork-accounts-');
      addTearDown(() => dir.delete(recursive: true));
      var downloads = 0;
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => MockClient((request) async {
          downloads++;
          return http.Response(request.url.queryParameters['api_key'] == 'first-token' ? 'first' : 'second', 200);
        }),
        cacheDirectoryProvider: () async => dir,
      );
      addTearDown(manager.dispose);
      Future<String> poster(String token) async {
        final url = 'https://jf.example/Items/1/Images/Primary?api_key=$token';
        final response = await manager.getFileStream(url).firstWhere((r) => r is FileInfo) as FileInfo;
        return response.file.readAsString();
      }

      expect(await poster('first-token'), 'first');
      expect(await poster('second-token'), 'second');
      expect(await poster('first-token'), 'first');
      expect(downloads, 2);
    });

    test('encoded credential values stay out of persistent metadata, including explicit URL keys', () async {
      final dir = await Directory.systemTemp.createTemp('artwork-query-');
      addTearDown(() => dir.delete(recursive: true));
      final inner = Uri.https('source.example', '/poster', {'X-Plex-Token': 'a/b+private-tail'});
      final url = Uri.https('images.example', '/photo/:/transcode', {'url': inner.toString()}).toString();
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => MockClient((request) async {
          expect(request.url.queryParameters['url'], inner.toString());
          return http.Response.bytes([1], 200);
        }),
        cacheDirectoryProvider: () async => dir,
      );
      final downloaded = await manager.getImageFile(url, key: url).firstWhere((r) => r is FileInfo) as FileInfo;
      final cached = await manager.getFileFromCache(url);
      await manager.dispose();
      expect(downloaded.originalUrl, isNot(contains('private-tail')));
      expect(cached?.originalUrl, downloaded.originalUrl);
      final metadata = [
        await for (final e in dir.list(recursive: true))
          if (e is File && e.path.endsWith('.hive')) utf8.decode(await e.readAsBytes(), allowMalformed: true),
      ];
      expect(metadata, isNotEmpty);
      expect(metadata.join(), isNot(contains('private-tail')));
    });

    test('header-authenticated requests keep distinct cached representations', () async {
      final dir = await Directory.systemTemp.createTemp('artwork-headers-');
      addTearDown(() => dir.delete(recursive: true));
      var requests = 0;
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => MockClient((request) async {
          requests++;
          return http.Response(request.headers['Authorization'] == 'first' ? 'A' : 'B', 200);
        }),
        cacheDirectoryProvider: () async => dir,
      );
      addTearDown(manager.dispose);
      Future<String> fetch(Map<String, String> headers) async {
        final result =
            await manager
                    .getFileStream('https://server.example/poster', key: 'poster', headers: headers)
                    .firstWhere((r) => r is FileInfo)
                as FileInfo;
        return result.file.readAsString();
      }

      expect(await fetch({'Authorization': 'first'}), 'A');
      expect(await fetch({'Authorization': 'second'}), 'B');
      expect(await fetch({'authorization': 'first'}), 'A');
      expect(requests, 2);
      expect(await manager.getFileFromCache('poster'), isNull);
    });

    test('existing account-qualified artwork remains readable offline after upgrade', () async {
      final dir = await Directory.systemTemp.createTemp('artwork-upgrade-');
      addTearDown(() => dir.delete(recursive: true));
      final legacy = ce_cache.DefaultCacheManager(cacheDirectoryProvider: () async => dir);
      final key = 'plex_optimized_${'a' * 40}';
      await legacy.putFile(plexUrl, [9], key: key);
      await legacy.dispose();
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => throw StateError('unexpected network request'),
        cacheDirectoryProvider: () async => dir,
      );
      addTearDown(manager.dispose);
      final restored = await manager.getFileStream(plexUrl, key: key).firstWhere((r) => r is FileInfo) as FileInfo;
      expect(await restored.file.readAsBytes(), [9]);
      expect(restored.originalUrl, isNot(contains(token)));
    });

    test('explicit cache keys support lookup and removal for directly stored artwork', () async {
      final dir = await Directory.systemTemp.createTemp('artwork-explicit-');
      addTearDown(() => dir.delete(recursive: true));
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => throw StateError('unexpected network request'),
        cacheDirectoryProvider: () async => dir,
      );
      addTearDown(manager.dispose);
      const key = 'cover?api_key=fixture-token';
      await manager.putFile(plexUrl, [42], key: key);
      final file = await manager.getFileFromCache(key);
      expect(await file?.file.readAsBytes(), [42]);
      expect(file?.originalUrl, isNot(contains(token)));
      await manager.removeFile(key);
      expect(await manager.getFileFromCache(key), isNull);
    });

    test('the cache stores the redacted URL but downloads with the token', () async {
      final dir = await Directory.systemTemp.createTemp('artwork_cache_test');
      addTearDown(() => dir.delete(recursive: true));
      final requests = <http.BaseRequest>[];
      final manager = PlexImageCacheManager.forTesting(
        httpClientFactory: () => MockClient((request) async {
          requests.add(request);
          return http.Response.bytes([1, 2, 3], 200);
        }),
        cacheDirectoryProvider: () async => dir,
      );

      // Default lookup and eviction still accept the original URL.
      final downloaded = await manager.getFileStream(plexUrl).firstWhere((r) => r is FileInfo) as FileInfo;
      final cached = await manager.getFileFromCache(plexUrl);
      await manager.dispose();

      expect(requests.single.url.queryParameters['X-Plex-Token'], token);
      expect(requests.single.url.queryParameters['url'], contains('X-Plex-Token=$token'));
      expect(requests.single.headers.keys.map((k) => k.toLowerCase()), isNot(contains('x-plezy-credentialed-url')));
      expect(downloaded.originalUrl, isNot(contains(token)));
      expect(cached?.originalUrl, downloaded.originalUrl);

      final persisted = <String>[];
      await for (final entity in dir.list(recursive: true)) {
        if (entity is File && entity.path.contains('hive')) {
          persisted.add(utf8.decode(await entity.readAsBytes(), allowMalformed: true));
        }
      }
      expect(persisted, isNotEmpty);
      expect(persisted.join(), isNot(contains(token)));
    });
  });
}
