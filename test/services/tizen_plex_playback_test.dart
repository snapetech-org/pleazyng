import 'dart:convert';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:plezy/database/app_database.dart';
import 'package:plezy/media/ids.dart';
import 'package:plezy/media/media_backend.dart';
import 'package:plezy/media/media_kind.dart';
import 'package:plezy/models/transcode_quality_preset.dart';
import 'package:plezy/services/playback_initialization_types.dart';
import 'package:plezy/services/plex_api_cache.dart';

import '../test_helpers/backend_client_fixtures.dart';
import '../test_helpers/media_items.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  group('Samsung Plex negotiation', () {
    late AppDatabase db;
    setUp(() {
      db = AppDatabase.forTesting(NativeDatabase.memory());
      PlexApiCache.initialize(db);
    });
    tearDown(() => db.close());

    for (final decisionWorks in [true, false]) {
      for (final source in ['h264', 'hevc', 'hevc-dv81', 'hevc-truehd']) {
        final compatible = source != 'hevc-truehd';
        final uhd = source != 'h264';
        final isDv81 = source == 'hevc-dv81';
        test(
          compatible
              ? 'Original preserves compatible $source video ($decisionWorks)'
              : decisionWorks
              ? 'Original preserves UHD video while converting TrueHD audio'
              : 'failed transcode cannot fall back to an unsupported original',
          () async {
            final decisions = <Uri>[];
            final client = testPlexClient(
              serverId: ServerId('tv-server'),
              handler: (request) async {
                if (request.url.path == '/library/metadata/42') {
                  return http.Response(
                    jsonEncode({
                      'MediaContainer': {
                        'Metadata': [
                          {
                            'ratingKey': '42',
                            'type': 'movie',
                            'title': 'Movie',
                            'Media': [
                              {
                                'id': 7,
                                'container': uhd ? 'mkv' : 'mp4',
                                'videoCodec': uhd ? 'hevc' : 'h264',
                                'width': uhd ? 3840 : 1920,
                                'height': uhd ? 2160 : 1080,
                                'Part': [
                                  {
                                    'id': 99,
                                    'key': '/library/parts/99/file.mkv',
                                    'Stream': [
                                      {
                                        'streamType': 1,
                                        'id': 300,
                                        'codec': uhd ? 'hevc' : 'h264',
                                        if (isDv81) ...{
                                          'bitDepth': 10,
                                          'DOVIPresent': 1,
                                          'DOVIProfile': 8,
                                          'DOVIBLCompatID': 1,
                                          'DOVIBLPresent': 1,
                                        },
                                      },
                                      {
                                        'streamType': 2,
                                        'id': 301,
                                        'codec': compatible ? 'aac' : 'truehd',
                                        'channels': compatible ? 2 : 8,
                                        'selected': true,
                                      },
                                    ],
                                  },
                                ],
                              },
                            ],
                          },
                        ],
                      },
                    }),
                    200,
                    headers: {'content-type': 'application/json'},
                  );
                }
                if (request.url.path.endsWith('/decision')) {
                  decisions.add(request.url);
                  if (!decisionWorks) return http.Response('unavailable', 500);
                  return http.Response(
                    jsonEncode({
                      'MediaContainer': {
                        'generalDecisionCode': 1001,
                        'transcodeDecisionCode': 1001,
                        'Metadata': [
                          {
                            'Media': [
                              {'container': 'mpegts', 'protocol': 'hls', 'selected': true},
                            ],
                          },
                        ],
                      },
                    }),
                    200,
                    headers: {'content-type': 'application/json'},
                  );
                }
                return http.Response('unexpected request', 404);
              },
            );
            addTearDown(client.close);
            final result = client.getPlaybackInitialization(
              PlaybackInitializationOptions(
                metadata: testMediaItem(
                  id: '42',
                  backend: MediaBackend.plex,
                  kind: MediaKind.movie,
                  serverId: 'tv-server',
                ),
                selectedMediaIndex: 0,
                requestCompatibleDolbyVisionRendition: isDv81,
                qualityPreset: TranscodeQualityPreset.original,
                sessionIdentifier: 'session',
                transcodeSessionId: 'transcode',
              ),
            );
            if (compatible) {
              expect((await result).isTranscoding, isFalse);
              expect(decisions, hasLength(1));
              expect(decisions.single.queryParameters['directPlay'], '1');
              expect(decisions.single.queryParameters['protocol'], 'http');
              expect(decisions.single.queryParameters, isNot(contains('videoResolution')));
              expect(decisions.single.queryParameters, isNot(contains('X-Plex-Client-Profile-Extra')));
              return;
            }
            if (decisionWorks) {
              expect((await result).isTranscoding, isTrue);
            } else {
              await expectLater(result, throwsA(isA<PlaybackException>()));
            }
            expect(decisions, isNotEmpty);
            final params = decisions.first.queryParameters;
            expect(params['directPlay'], '0');
            expect(params['directStream'], '1');
            expect(params['directStreamAudio'], '0');
            expect(params, isNot(contains('videoResolution')));
            final profile = params['X-Plex-Client-Profile-Extra']!;
            expect(profile, contains('container=mpegts&videoCodec=h264%2Chevc%2Cmpeg2video&audioCodec=aac)'));
            expect(profile, contains('name=audio.channels&value=2'));
            expect(profile, isNot(contains('ac3')));
          },
        );
      }
    }
  }, skip: !const bool.fromEnvironment('PLEZY_TIZEN'));
}
