import 'dart:typed_data';

import '../media/media_part_timeline.dart';
import '../utils/app_logger.dart';
import '../utils/platform_detector.dart';
import 'scrub_preview_source.dart';

part 'bif_index.dart';

/// Keeps the downloaded BIF and reads its on-disk index directly while scrubbing.
class BifThumbnailService implements ScrubPreviewSource {
  _BifIndex? _index;
  double? _aspectRatio;
  int _generation = 0;

  static int get _maxFileBytes => PlatformDetector.isDesktopOS() ? 200 * 1024 * 1024 : 50 * 1024 * 1024;

  /// Download a Plex or Emby preview. The transport enforces the budget as bytes arrive.
  Future<void> load(Future<Uint8List?> Function(int maxBytes) fetchBytes, {double? aspectRatio, int? maxBytes}) async {
    final generation = ++_generation;
    final limit = (maxBytes ?? _maxFileBytes).clamp(0, _maxFileBytes);
    _aspectRatio = aspectRatio;
    _index = null;
    if (limit == 0) return;
    try {
      final bytes = await fetchBytes(limit);
      if (generation != _generation || bytes == null || bytes.length > limit) return;
      final retained = bytes.buffer.lengthInBytes > limit ? Uint8List.fromList(bytes) : bytes;
      _index = _BifIndex.parse(retained);
    } catch (e) {
      appLogger.w('BIF download/parse failed', error: e);
    }
  }

  /// The whole stacked movie shares the same budget as a single file. Give
  /// the playing part first claim and keep the other parts on the item clock.
  static Future<StackedScrubPreviewSource?> loadStacked(
    MediaPartTimeline timeline,
    Future<Uint8List?> Function(int partId, int maxBytes) fetch, {
    double? aspectRatio,
    int? maxBytes,
  }) async {
    var remaining = (maxBytes ?? _maxFileBytes).clamp(0, _maxFileBytes);
    final sources = List<BifThumbnailService?>.filled(timeline.parts.length, null);
    for (var n = 0; n < sources.length && remaining > 0; n++) {
      final i = (timeline.currentIndex + n) % sources.length;
      final partId = int.tryParse(timeline.parts[i].partId);
      if (partId == null) continue;
      final service = BifThumbnailService();
      await service.load((limit) => fetch(partId, limit), aspectRatio: aspectRatio, maxBytes: remaining);
      final index = service._index;
      if (index == null) continue;
      remaining -= index.data.buffer.lengthInBytes;
      sources[i] = service;
    }
    return sources.any((s) => s != null) ? StackedScrubPreviewSource(timeline: timeline, sources: sources) : null;
  }

  @override
  bool get isAvailable => _index != null;

  @override
  ScrubFrame? getFrame(Duration time) {
    final bytes = getThumbnail(time);
    return bytes == null ? null : BytesScrubFrame(bytes, aspectRatio: _aspectRatio ?? 16 / 9);
  }

  Uint8List? getThumbnail(Duration time) => _index?.frameAt(time);

  @override
  void dispose() {
    _generation++;
    _index = null;
  }
}
