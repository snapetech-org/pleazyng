import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/media/media_display_criteria.dart';
import 'package:plezy/media/runtime_playback_capabilities.dart';
import 'package:plezy/services/settings_service.dart';
import 'package:plezy/services/video_decode_capabilities.dart';
import 'package:plezy/utils/device_channel.dart';

import '../test_helpers/prefs.dart';

void main() {
  final binding = TestWidgetsFlutterBinding.ensureInitialized();

  /// Answers `com.plezy/device` with [reply] and records each call. A null
  /// [reply] leaves the method unimplemented, which is what desktop and a
  /// stale native build look like.
  List<String> stubProbe(Object? reply) {
    final calls = <String>[];
    binding.defaultBinaryMessenger.setMockMethodCallHandler(deviceChannel, (call) async {
      calls.add(call.method);
      if (reply == null) throw MissingPluginException();
      if (reply is PlatformException) throw reply;
      return reply;
    });
    addTearDown(() => binding.defaultBinaryMessenger.setMockMethodCallHandler(deviceChannel, null));
    return calls;
  }

  bool acceptsHevc() => VideoDecodeCapabilities.accepts(RankedVideoCodec.hevc);
  bool acceptsAv1() => VideoDecodeCapabilities.accepts(RankedVideoCodec.av1);

  setUp(() {
    VideoDecodeCapabilities.debugReset();
    addTearDown(VideoDecodeCapabilities.debugReset);
  });

  test('advertises both codecs before the probe has run', () {
    expect(acceptsHevc(), isTrue);
    expect(acceptsAv1(), isTrue);
    expect(VideoDecodeCapabilities.describeSync(), 'unknown');
  });

  test('reports the decoders the platform found', () async {
    stubProbe(<String, Object?>{
      'hevc': true,
      'av1': false,
      'dolbyVisionProfiles': [5, 7, 8],
    });

    await VideoDecodeCapabilities.getInstance();

    expect(acceptsHevc(), isTrue);
    expect(acceptsAv1(), isFalse);
    expect(VideoDecodeCapabilities.describeSync(), 'hevc=hw av1=none');
    expect(VideoDecodeCapabilities.supportedNativeDolbyVisionProfiles, {5, 7, 8});
    expect(VideoDecodeCapabilities.describeDolbyVisionProfilesSync(), '5,7,8');
  });

  test('a system player preserves installed source codecs and requests accelerated transcode targets', () async {
    final channel = Platform.isWindows
        ? const MethodChannel('plezy/window')
        : const MethodChannel('org.snapetech.pleazyng/system_video');
    binding.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (call) async => {
        'video': ['h264', 'hevc', 'vp9'],
        'hardware': ['hevc'],
        'containers': ['mp4', 'mkv'],
      },
    );
    addTearDown(() => binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await VideoDecodeCapabilities.getInstance();
    RuntimePlaybackCapabilities.withVideoEngine('gstreamer', () {
      expect(VideoDecodeCapabilities.directPlayVideoCodecs, containsAll(['hevc', 'vp9']));
      expect(VideoDecodeCapabilities.transcodeVideoCodecs, [RankedVideoCodec.hevc, RankedVideoCodec.h264]);
      expect(VideoDecodeCapabilities.systemContainers, {'mp4', 'mkv'});
      expect(VideoDecodeCapabilities.canOpenHdr10BaseLayer('MKV'), isTrue);
      expect(VideoDecodeCapabilities.canOpenHdr10BaseLayer('ts'), isFalse);
      expect(VideoDecodeCapabilities.sourceDecoderUnavailable('av1'), isTrue);
      expect(VideoDecodeCapabilities.sourceDecoderUnavailable('h265'), isFalse);
      expect(VideoDecodeCapabilities.sourceDecoderUnavailable('theora'), isFalse);
    });
  });

  test('HDR10 base-layer routing uses the active backend rather than the desktop host', () {
    bool canOpen(String engine, String container) => RuntimePlaybackCapabilities.withVideoEngine(
      engine,
      () => VideoDecodeCapabilities.canOpenHdr10BaseLayer(container),
    );

    expect(canOpen('mpv', 'mkv'), isTrue);
    expect(canOpen('tvmpv', 'mkv'), isTrue);
    expect(canOpen('tizen', 'mkv'), isTrue);
    expect(canOpen('tizen', 'webm'), isFalse);
    expect(canOpen('gstreamer', 'mkv'), isFalse);
    expect(canOpen('mediaplayer', 'mkv'), isFalse);
    expect(canOpen('webos', 'mkv'), isFalse);
  });

  test('a missing codec entry counts as no hardware decoder', () async {
    stubProbe(<String, Object?>{'hevc': true});

    await VideoDecodeCapabilities.getInstance();

    expect(acceptsAv1(), isFalse);
  });

  // Keep the default mpv transcode list when the platform probe is unavailable.
  test('an unanswered probe keeps both codecs advertised', () async {
    stubProbe(null);

    await VideoDecodeCapabilities.getInstance();

    expect(acceptsHevc(), isTrue);
    expect(acceptsAv1(), isTrue);
    expect(VideoDecodeCapabilities.describeSync(), 'unprobed');
  });

  test('a failed probe keeps both codecs advertised', () async {
    stubProbe(PlatformException(code: 'error', message: 'MediaCodecList exploded'));

    await VideoDecodeCapabilities.getInstance();

    expect(acceptsHevc(), isTrue);
    expect(acceptsAv1(), isTrue);
    expect(VideoDecodeCapabilities.describeSync(), 'unprobed');
    expect(VideoDecodeCapabilities.supportedNativeDolbyVisionProfiles, isEmpty);
    expect(VideoDecodeCapabilities.describeDolbyVisionProfilesSync(), 'unreported');
  });

  test('an older native build with no Dolby Vision profile field fails closed', () async {
    stubProbe(<String, Object?>{'hevc': true, 'av1': true});

    await VideoDecodeCapabilities.getInstance();

    expect(VideoDecodeCapabilities.supportedNativeDolbyVisionProfiles, isEmpty);
    expect(VideoDecodeCapabilities.describeDolbyVisionProfilesSync(), 'unreported');
  });

  test('the probe runs once and is shared by concurrent callers', () async {
    final calls = stubProbe(<String, Object?>{'hevc': false, 'av1': false});

    final instances = await (VideoDecodeCapabilities.getInstance(), VideoDecodeCapabilities.getInstance()).wait;

    expect(calls, ['getVideoDecodeCapabilities']);
    expect(identical(instances.$1, instances.$2), isTrue);
    expect(acceptsHevc(), isFalse);
    expect(VideoDecodeCapabilities.describeSync(), 'hevc=none av1=none');
  });

  test('ExoPlayer uses Android codec enumeration and checks the selected format', () async {
    RuntimePlaybackCapabilities.activeVideoEngine = 'exoplayer';
    addTearDown(RuntimePlaybackCapabilities.reset);
    binding.defaultBinaryMessenger.setMockMethodCallHandler(deviceChannel, (call) async {
      if (call.method == 'getVideoDecodeCapabilities') {
        return {
          'hevc': true,
          'av1': false,
          'videoCodecs': ['h264', 'hevc', 'vp9'],
        };
      }
      expect(call.method, 'isNativeVideoFormatSupported');
      expect(call.arguments, {
        'codec': 'hevc',
        'width': 3840,
        'height': 2160,
        'fps': 59.94,
        'profile': 'Main 10',
        'bitDepth': 10,
      });
      return false;
    });
    addTearDown(() => binding.defaultBinaryMessenger.setMockMethodCallHandler(deviceChannel, null));
    await VideoDecodeCapabilities.getInstance();
    expect(VideoDecodeCapabilities.directPlayVideoCodecs, ['h264', 'hevc', 'vp9']);
    expect(VideoDecodeCapabilities.canOpenHdr10BaseLayer('mkv'), isTrue);
    expect(VideoDecodeCapabilities.canOpenHdr10BaseLayer('avi'), isFalse);
    expect(
      await VideoDecodeCapabilities.sourceFormatUnavailable(
        'h265',
        const MediaDisplayCriteria(width: 3840, height: 2160, fps: 59.94, codecProfile: 'Main 10', bitDepth: 10),
      ),
      isTrue,
    );
  });

  group('user refusals (#2443)', () {
    setUp(() async {
      resetSharedPreferencesForTest();
      SettingsService.resetForTesting();
      await SettingsService.getInstance();
    });

    Future<void> refuse(List<String> ids) => SettingsService.instance.write(SettingsService.refusedVideoCodecs, ids);

    // Desktop's probe never answers, so a refusal is the only thing that can
    // narrow the list there — and it must, or a weak machine keeps being
    // handed a codec it cannot decode.
    test('a refused codec is dropped without narrowing the ranked order', () async {
      stubProbe(null);
      await VideoDecodeCapabilities.getInstance();
      await refuse(['hevc']);

      expect(acceptsHevc(), isFalse);
      expect(acceptsAv1(), isTrue);
      expect(VideoDecodeCapabilities.transcodeVideoCodecs, [RankedVideoCodec.av1, RankedVideoCodec.h264]);
    });

    test('H.264 stays accepted even when stored as refused', () async {
      await refuse(['av1', 'hevc', 'h264']);

      expect(VideoDecodeCapabilities.isRefusedByUser(RankedVideoCodec.h264), isFalse);
      expect(VideoDecodeCapabilities.transcodeVideoCodecs, [RankedVideoCodec.h264]);
    });

    test('a refusal and a missing hardware decoder each remove their own codec', () async {
      stubProbe(<String, Object?>{'hevc': true, 'av1': false});
      await VideoDecodeCapabilities.getInstance();
      await refuse(['hevc']);

      expect(VideoDecodeCapabilities.transcodeVideoCodecs, [RankedVideoCodec.h264]);
      // Only the refusal counts as the user's: Plex gates direct play on it alone.
      expect(VideoDecodeCapabilities.isRefusedByUser(RankedVideoCodec.hevc), isTrue);
      expect(VideoDecodeCapabilities.isRefusedByUser(RankedVideoCodec.av1), isFalse);
    });

    test('a changed refusal applies without re-probing', () async {
      await refuse(['av1']);
      expect(acceptsAv1(), isFalse);

      await refuse(const []);
      expect(acceptsAv1(), isTrue);
    });
  });
}
