import 'dart:math' as math;

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:plezy/database/app_database.dart';
import 'package:plezy/media/media_backend.dart';
import 'package:plezy/media/media_server_client.dart';
import 'package:plezy/services/music/music_playlist_loader.dart';
import 'package:plezy/services/playlist_items_loader.dart';
import 'package:plezy/services/plex_api_cache.dart';

import '../test_helpers/backend_client_fixtures.dart';
import '../test_helpers/http_fixtures.dart';

void main() {
  late AppDatabase db;
  setUp(() {
    db = AppDatabase.forTesting(NativeDatabase.memory());
    PlexApiCache.initialize(db);
  });
  tearDown(() => db.close());

  for (final backend in [MediaBackend.plex, MediaBackend.jellyfin, MediaBackend.emby]) {
    final plex = backend == MediaBackend.plex;
    MediaServerClient clientFor(Future<http.Response> Function(http.Request) handler) {
      final client = plex
          ? testPlexClient(handler: handler)
          : testJellyfinClient(
              connection: backend == MediaBackend.emby ? testEmbyConnection() : null,
              handler: handler,
            );
      addTearDown(client.close);
      return client;
    }

    for (final length in [400, 403]) {
      test('$backend preserves $length ordered playlist entries without a server count', () async {
        final client = clientFor((request) async {
          final params = request.url.queryParameters;
          final start = int.parse(params[plex ? 'X-Plex-Container-Start' : 'StartIndex']!);
          final size = int.parse(params[plex ? 'X-Plex-Container-Size' : 'Limit']!);
          final rows = [
            for (int i = start; i < math.min(start + size, length); i++)
              if (plex)
                {'ratingKey': '${i % 7}', 'type': 'track', 'playlistItemID': i + 1}
              else
                {'Id': '${i % 7}', 'Type': 'Audio', 'PlaylistItemId': '${i + 1}'},
          ];
          return jsonResponse(
            plex
                ? {
                    'MediaContainer': {'size': rows.length, 'Metadata': rows},
                  }
                : {'Items': rows},
          );
        });
        final expected = List.generate(length, (i) => '${i % 7}');
        expect((await fetchAllPlaylistItems(client, '42', requireComplete: true)).map((i) => i.id), expected);
        final read = MusicPlaylistRead(client, '42', current: () => true, maxItems: length);
        await read.window(0, active: () => true);
        expect((await read.all(active: () => true)).map((i) => i.id), expected);
      });
    }

    test('$backend stops a repeated playlist page even without a reported count or offset', () async {
      final requests = <int>[];
      final client = clientFor((request) async {
        final start = int.parse(request.url.queryParameters[plex ? 'X-Plex-Container-Start' : 'StartIndex']!);
        requests.add(start);
        final rows = [
          if (start < 4)
            for (var i = 1; i <= 2; i++)
              if (plex)
                {'ratingKey': '7', 'type': 'track', 'playlistItemID': i}
              else
                {'Id': '7', 'Type': 'Audio', 'PlaylistItemId': '$i'},
        ];
        return jsonResponse(
          plex
              ? {
                  'MediaContainer': {'Metadata': rows},
                }
              : {'Items': rows},
        );
      });
      await expectLater(fetchAllPlaylistItems(client, '42', pageSize: 2, requireComplete: true), throwsFormatException);
      expect(requests, [0, 2]);
    });

    test('$backend rejects membership with a missing track identity', () async {
      final client = clientFor(
        (_) async => jsonResponse(
          plex
              ? {
                  'MediaContainer': {
                    'Metadata': [
                      {'type': 'track'},
                    ],
                  },
                }
              : {
                  'Items': [
                    {'Type': 'Audio'},
                  ],
                },
        ),
      );
      await expectLater(fetchAllPlaylistItems(client, '42', requireComplete: true), throwsFormatException);
    });

    test('$backend rejects an unrecognizable reply and accepts a confirmed empty playlist', () async {
      var empty = false;
      final client = clientFor(
        (_) async => jsonResponse(
          empty
              ? (plex
                    ? {
                        'MediaContainer': {'size': 0},
                      }
                    : {'Items': []})
              : {'message': 'Please retry'},
        ),
      );
      await expectLater(fetchAllPlaylistItems(client, '42', requireComplete: true), throwsFormatException);
      empty = true;
      expect(await fetchAllPlaylistItems(client, '42', requireComplete: true), isEmpty);
    });
  }
}
