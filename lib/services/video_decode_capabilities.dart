import 'dart:io';

import 'package:device_info_plus_webos/device_info_plus_webos.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../media/codec_catalog.dart';
import '../media/decoder_inventory.dart';
import '../media/media_display_criteria.dart';
import '../media/runtime_playback_capabilities.dart';
import '../media/webos_playback_profile.dart';
import '../media/tizen_playback_profile.dart';

import '../utils/async_singleton.dart';
import '../utils/device_channel.dart';
import '../utils/platform_detector.dart';
import 'settings_service.dart';

/// The video codecs Plezy negotiates with a media server, most efficient
/// first. Every transcode target lists them in this order so a server able to
/// encode more than one picks the best; the hardware probe and the user's
/// refusals only remove entries, never reorder them.
enum RankedVideoCodec {
  av1('av1'),
  hevc('hevc'),
  h264('h264');

  const RankedVideoCodec(this.id);

  /// Codec name on the wire and in [SettingsService.refusedVideoCodecs].
  final String id;

  /// H.264 is the one output every server can encode to. Refusing it would
  /// leave a transcode with nothing to produce, so it is always accepted.
  bool get isRefusable => this != h264;

  /// The entry a server-reported codec name belongs to, aliases included, or
  /// null for a codec outside the ranked list (VP9, MPEG-2, …).
  static RankedVideoCodec? fromServerCodec(String? codec) =>
      switch (codec == null ? null : CodecCatalog.normalize(codec)) {
        'av1' => av1,
        'hevc' || 'h265' || 'hev1' => hevc,
        'h264' || 'avc' || 'avc1' => h264,
        _ => null,
      };
}

/// Native decoder evidence and server negotiation for the active video engine.
class VideoDecodeCapabilities {
  VideoDecodeCapabilities._();

  static final AsyncSingleton<VideoDecodeCapabilities> _singleton = AsyncSingleton();

  /// Null until the platform answers, so [describeSync] can tell a measured
  /// "yes" from an assumed one. Both are set together or not at all.
  bool? _hardwareHevc;
  bool? _hardwareAv1;
  Set<String>? _androidVideoCodecs;
  Set<String>? _systemVideoCodecs;
  Set<String> _systemHardwareCodecs = const {};
  Set<String> _systemContainers = const {'mp4'};
  bool? _webosUhd;
  int? _webosSdkMajor;
  bool? _webosDolbyVision;
  bool? _webosHdr10;
  Set<int> _nativeDolbyVisionProfiles = const {};
  bool _dolbyVisionProfilesReported = false;

  /// Get the singleton, probing the platform's decoders on first call.
  static Future<VideoDecodeCapabilities> getInstance() =>
      _singleton.getInstance(VideoDecodeCapabilities._, (instance) => instance._detect());

  Future<void> _detect() async {
    if (PlatformDetector.isTizenBuild()) {
      try {
        const channel = MethodChannel('org.snapetech.pleazyng/system_video');
        final codecs = await channel.invokeListMethod<String>('hardwareVideoDecoders');
        if (codecs != null && codecs.isNotEmpty) _hardwareHevc = codecs.contains('hevc');
      } on MissingPluginException {
        // Older plugin builds only have the compatibility transcode target.
      } on PlatformException {
        // The firmware's media-codec service is optional.
      }
      return;
    }
    if (PlatformDetector.isWebOSBuild()) {
      try {
        // webOS reports Linux to dart:io. The generic Linux getter casts
        // this plugin's WebOSDeviceInfo to LinuxDeviceInfo.
        final info = await DeviceInfoWebOSPlugin().webosInfo;
        _webosDolbyVision = info.dolbyVision;
        _webosHdr10 = info.hdr10;
        _webosUhd = info.uhd ?? _webosUhd;
        _webosSdkMajor = int.tryParse(info.sdkVersion?.split('.').first ?? '');
      } on MissingPluginException {
        // Keep the compatibility profile until firmware supplies device info.
      } on PlatformException {
        // Keep the compatibility profile if the native service is unavailable.
      }
      return;
    }
    if (Platform.isLinux || Platform.isWindows || Platform.isMacOS || Platform.isIOS) {
      final channel = Platform.isWindows
          ? const MethodChannel('plezy/window')
          : const MethodChannel('org.snapetech.pleazyng/system_video');
      try {
        final result = await channel.invokeMapMethod<String, dynamic>('decoderInventory');
        if (result?['video'] case final List codecs) {
          _systemVideoCodecs = codecs.whereType<String>().map(CodecCatalog.normalize).toSet();
          _systemHardwareCodecs = (result?['hardware'] as List? ?? []).whereType<String>().toSet();
          if (result?['containers'] case final List containers) {
            _systemContainers = containers.whereType<String>().toSet();
          }
        }
      } on MissingPluginException {
        // An older player has the baseline profile.
      } on PlatformException {
        // The decoder registry may be unavailable during startup.
      }
    }
    try {
      final result = await deviceChannel.invokeMapMethod<String, dynamic>('getVideoDecodeCapabilities');
      if (result != null) {
        _hardwareHevc = result['hevc'] == true;
        _hardwareAv1 = result['av1'] == true;
        final codecs = result['videoCodecs'];
        if (codecs is List && codecs.every((entry) => entry is String && entry.isNotEmpty)) {
          _androidVideoCodecs = codecs.cast<String>().map(CodecCatalog.normalize).toSet();
        }
        _nativeDolbyVisionProfiles = _parseDolbyVisionProfiles(result['dolbyVisionProfiles']);
        _dolbyVisionProfilesReported = result.containsKey('dolbyVisionProfiles');
      }
    } on MissingPluginException {
      // Desktop, or a stale native build — keep advertising both.
    } on PlatformException {
      // Decoder enumeration failed — keep advertising both.
    }
  }

  Set<int> _parseDolbyVisionProfiles(Object? raw) {
    if (raw is! Iterable) return const {};
    return {
      for (final value in raw)
        if (value is int) value else ?int.tryParse(value.toString()),
    };
  }

  /// Whether [codec] should be advertised to a media server. Safe before init.
  static bool accepts(RankedVideoCodec codec) {
    if (isRefusedByUser(codec)) return false;
    final instance = _singleton.instance;
    if (const {'gstreamer', 'mediafoundation', 'avplayer'}.contains(videoEngine) &&
        instance?._systemVideoCodecs != null) {
      return codec == RankedVideoCodec.h264 || instance!._systemHardwareCodecs.contains(codec.id);
    }
    if (videoEngine == 'gstreamer' || videoEngine == 'mediafoundation') return codec == RankedVideoCodec.h264;
    if (videoEngine == 'tvmpv' || videoEngine == 'tizen') {
      return codec == RankedVideoCodec.h264 ||
          (codec == RankedVideoCodec.hevc &&
              (_singleton.instance?._hardwareHevc == true ||
                  (videoEngine == 'tvmpv' && _singleton.instance?._webosUhd == true)));
    }
    if (videoEngine == 'avplayer' && codec != RankedVideoCodec.h264) {
      return codec == RankedVideoCodec.hevc && _singleton.instance?._hardwareHevc == true;
    }
    if (videoEngine == 'webos') return codec != RankedVideoCodec.av1 || _webosModernUhd;
    if (videoEngine == 'exoplayer' || videoEngine == 'mediaplayer') {
      return codec == RankedVideoCodec.h264 || (_singleton.instance?._androidVideoCodecs?.contains(codec.id) ?? false);
    }
    return switch (codec) {
      RankedVideoCodec.av1 => instance?._hardwareAv1 ?? true,
      RankedVideoCodec.hevc => instance?._hardwareHevc ?? true,
      RankedVideoCodec.h264 => true,
    };
  }

  /// Transcode outputs to offer, in [RankedVideoCodec] order. Never empty:
  /// H.264 cannot be refused.
  static List<RankedVideoCodec> get transcodeVideoCodecs => [
    for (final codec in RankedVideoCodec.values)
      if (accepts(codec)) codec,
  ];

  static String get videoEngine => RuntimePlaybackCapabilities.negotiationEngine == 'tvmpv'
      ? 'tvmpv'
      : PlatformDetector.isTizenBuild()
      ? 'tizen'
      : PlatformDetector.isWebOSBuild()
      ? 'webos'
      : RuntimePlaybackCapabilities.negotiationEngine;

  static bool? webosNativeDolbyVision(String engine) =>
      engine == 'webos' ? _singleton.instance?._webosDolbyVision : null;
  static bool webosNativeHdr10(String engine) => engine == 'webos' && _singleton.instance?._webosHdr10 == true;

  /// An HDR10-compatible HEVC base layer can be played by the active
  /// backend even when it does not advertise Dolby Vision.
  static bool canOpenHdr10BaseLayer(String? container) {
    if (isRefusedByUser(RankedVideoCodec.hevc)) return false;
    final format = container?.toLowerCase();
    return switch (videoEngine) {
      'mpv' || 'tvmpv' => !sourceDecoderUnavailable('hevc'),
      'tizen' => tizenVideoContainers.entries.any(
        (entry) => entry.key.split(',').contains(format) && entry.value.contains('hevc'),
      ),
      'exoplayer' || 'mediaplayer' =>
        const {'mp4', 'm4v', 'mkv', 'ts', 'mpegts'}.contains(format) && directPlayVideoCodecs.contains('hevc'),
      'avplayer' ||
      'mediafoundation' ||
      'gstreamer' => systemContainers.contains(format) && directPlayVideoCodecs.contains('hevc'),
      // LG's native route requires HDR10 capability from the television.
      _ => false,
    };
  }

  static bool get _webosModernUhd =>
      _singleton.instance?._webosUhd == true && (_singleton.instance?._webosSdkMajor ?? 0) >= 9;

  static Map<String, Set<String>> get webosDirectPlayContainers => webosVideoContainers(modernUhd: _webosModernUhd);

  /// Original-source codecs and output encoders have different requirements.
  /// Until mpv enumeration arrives, offer the compatibility baseline and any
  /// codec positively reported by the platform hardware probe.
  static List<String> get directPlayVideoCodecs {
    switch (videoEngine) {
      case 'tvmpv':
        return const [
          'h264',
          'hevc',
          'mpeg2video',
          'mpeg4',
          'vp8',
          'vp9',
          'av1',
          'theora',
          'mjpeg',
          'wmv3',
          'vc1',
          'h263',
          'flv1',
        ].where(_allowSourceCodec).toList();
      case 'tizen':
        return tizenVideoContainers.values.expand((codecs) => codecs).toSet().where(_allowSourceCodec).toList();
      case 'mediafoundation':
      case 'gstreamer':
      case 'avplayer':
        final measured = _singleton.instance?._systemVideoCodecs;
        if (measured != null) return measured.where(_allowSourceCodec).toList();
        return ['h264', if (videoEngine == 'avplayer' && accepts(RankedVideoCodec.hevc)) 'hevc'];
      case 'webos':
        return webosDirectPlayContainers.values.expand((codecs) => codecs).toSet().where(_allowSourceCodec).toList();
      case 'mediaplayer':
      case 'exoplayer':
        return (_singleton.instance?._androidVideoCodecs ?? const {'h264'}).where(_allowSourceCodec).toList();
    }
    final measured = RuntimePlaybackCapabilities.videoDecoders;
    final instance = _singleton.instance;
    final candidates = <String>{
      'hevc',
      'h264',
      'av1',
      if (measured != null) ...measured.decoders.keys.where(CodecCatalog.videoCodecs.contains),
    };
    return [
      for (final codec in candidates)
        if (_mpvCanDirectPlay(codec, measured, instance)) codec,
    ];
  }

  static Set<String> get systemContainers => _singleton.instance?._systemContainers ?? const {'mp4'};

  static bool _mpvCanDirectPlay(String codec, DecoderInventory? measured, VideoDecodeCapabilities? instance) {
    final ranked = RankedVideoCodec.fromServerCodec(codec);
    final hasHardware = switch (ranked) {
      RankedVideoCodec.hevc => instance?._hardwareHevc == true,
      RankedVideoCodec.av1 => instance?._hardwareAv1 == true,
      _ => false,
    };
    if (ranked != null && !accepts(ranked)) return false;
    if (measured == null) return codec == 'h264' || hasHardware;
    return measured.hasSoftwareDecoder(codec) || (hasHardware && measured.contains(codec));
  }

  static bool _allowSourceCodec(String codec) {
    final ranked = RankedVideoCodec.fromServerCodec(codec);
    return ranked == null || !isRefusedByUser(ranked);
  }

  static bool sourceDecoderUnavailable(String? codec) {
    if (codec == null || codec.trim().isEmpty) return false;
    final system = _singleton.instance?._systemVideoCodecs;
    if (system != null && const {'gstreamer', 'mediafoundation', 'avplayer'}.contains(videoEngine)) {
      final normalized = CodecCatalog.normalize(codec);
      // All three probes cover the ranked delivery codecs. Other originals
      // may be playable even when their format was outside this probe.
      return RankedVideoCodec.fromServerCodec(normalized) != null && !system.contains(normalized);
    }
    if (videoEngine != 'mpv') return false;
    final measured = RuntimePlaybackCapabilities.videoDecoders;
    return measured != null && !measured.contains(codec);
  }

  /// Android validates the selected stream against a single platform decoder's
  /// profile, dimensions and rate together. Dolby Vision has its own routing
  /// and ExoPlayer can recover via mpv when the platform cannot answer.
  static Future<bool> sourceFormatUnavailable(String? codec, MediaDisplayCriteria? source, {String? container}) async {
    if (sourceDecoderUnavailable(codec)) return true;
    if (videoEngine == 'mpv' && codec != null) {
      final normalized = CodecCatalog.normalize(codec);
      if (normalized == 'av2') return RuntimePlaybackCapabilities.mpvAv2ContainerUnavailable(container);
      return RuntimePlaybackCapabilities.mpvElementaryContainerUnavailable(normalized, container);
    }
    if (videoEngine == 'webos' && codec != null && source != null && !source.hasDolbyVision) {
      return webosVideoFormatUnavailable(CodecCatalog.normalize(codec), source, uhd: _singleton.instance?._webosUhd);
    }
    if (videoEngine != 'exoplayer' || codec == null || source == null || source.hasDolbyVision) return false;
    try {
      final supported = await deviceChannel.invokeMethod<bool>('isNativeVideoFormatSupported', {
        'codec': CodecCatalog.normalize(codec),
        'width': source.width,
        'height': source.height,
        'fps': source.fps,
        'profile': source.codecProfile,
        'bitDepth': source.bitDepth,
      });
      return supported == false;
    } on MissingPluginException {
      return false;
    } on PlatformException {
      return false;
    }
  }

  /// Platform-reported profiles that the current native video path can accept.
  /// This is a decoder/output capability claim, not a pixel-accuracy result.
  static Set<int> get supportedNativeDolbyVisionProfiles =>
      Set<int>.unmodifiable(_singleton.instance?._nativeDolbyVisionProfiles ?? const {});

  /// Distinguishes a measured empty profile set from a missing/failed probe.
  static String describeDolbyVisionProfilesSync() {
    final instance = _singleton.instance;
    if (videoEngine == 'webos') {
      return 'profiles unreported, native DV capability: ${webosNativeDolbyVision(videoEngine) ?? 'unknown'}';
    }
    if (instance == null || !instance._dolbyVisionProfilesReported) return 'unreported';
    final profiles = instance._nativeDolbyVisionProfiles.toList()..sort();
    return profiles.isEmpty ? 'none' : profiles.join(',');
  }

  /// Whether the user refused [codec] in settings, independent of the probe.
  static bool isRefusedByUser(RankedVideoCodec codec) {
    if (!codec.isRefusable) return false;
    final settings = SettingsService.instanceOrNull;
    return settings != null && settings.read(SettingsService.refusedVideoCodecs).contains(codec.id);
  }

  /// One-line summary for the startup log and bug-report headers, e.g.
  /// `hevc=hw av1=none` on an Apple TV 4K, or `unprobed` on desktop, with
  /// ` refused=hevc` appended when the user refused a codec. Answers "did
  /// this device really report an AV1 decoder" when a transcode stutters.
  static String describeSync() {
    final instance = _singleton.instance;
    if (instance == null) return 'unknown';
    final hevc = instance._hardwareHevc;
    final av1 = instance._hardwareAv1;
    final probe = hevc == null || av1 == null ? 'unprobed' : 'hevc=${hevc ? 'hw' : 'none'} av1=${av1 ? 'hw' : 'none'}';
    final refused = [
      for (final codec in RankedVideoCodec.values)
        if (isRefusedByUser(codec)) codec.id,
    ];
    return refused.isEmpty ? probe : '$probe refused=${refused.join(',')}';
  }

  /// Test-only: drop the memoized probe, optionally seeding measured results
  /// so the sync accessors can be exercised without a platform channel.
  @visibleForTesting
  static void debugReset({bool? hardwareHevc, bool? hardwareAv1, Set<int>? supportedNativeDolbyVisionProfiles}) {
    if (hardwareHevc == null && hardwareAv1 == null && supportedNativeDolbyVisionProfiles == null) {
      _singleton.debugReset();
      return;
    }
    final instance = _singleton.instance ?? VideoDecodeCapabilities._();
    _singleton.debugReset(instance: instance);
    instance._hardwareHevc = hardwareHevc;
    instance._hardwareAv1 = hardwareAv1;
    if (supportedNativeDolbyVisionProfiles != null) {
      instance._nativeDolbyVisionProfiles = Set<int>.of(supportedNativeDolbyVisionProfiles);
      instance._dolbyVisionProfilesReported = true;
    }
  }
}
