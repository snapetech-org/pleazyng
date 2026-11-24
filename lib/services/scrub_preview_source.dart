import 'dart:typed_data';

import 'package:flutter/widgets.dart';

import '../media/media_part_timeline.dart';
import '../media/media_source_info.dart';

/// One seek-preview frame, either a Plex/Emby JPEG or a tile in a Jellyfin
/// sprite sheet. The tooltip uses [aspectRatio] to size the image.
sealed class ScrubFrame {
  const ScrubFrame();

  /// Source frame aspect ratio (width / height). The tooltip uses this to
  /// pick its own height; the renderer assumes the box already matches.
  double get aspectRatio;
}

/// Plex BIF: standalone JPEG bytes ready for [Image.memory].
class BytesScrubFrame extends ScrubFrame {
  final Uint8List bytes;
  @override
  final double aspectRatio;
  const BytesScrubFrame(this.bytes, {this.aspectRatio = 16 / 9});
}

/// Jellyfin Trickplay: a single sprite sheet plus the position of the tile
/// to display within it. Aspect comes from [sourceTileSize]; the tooltip
/// adopts that aspect so each source tile fills the box exactly.
class SheetScrubFrame extends ScrubFrame {
  final ImageProvider sheet;
  final int tileColumn;
  final int tileRow;
  final int sheetColumns;
  final int sheetRows;
  final Size sourceTileSize;
  const SheetScrubFrame({
    required this.sheet,
    required this.tileColumn,
    required this.tileRow,
    required this.sheetColumns,
    required this.sheetRows,
    required this.sourceTileSize,
  });

  @override
  double get aspectRatio => sourceTileSize.width / sourceTileSize.height;
}

/// Backend-neutral source of [ScrubFrame]s for the timeline tooltip.
/// Plex implements via BIF bytes; Jellyfin via Trickplay sheet crops.
abstract class ScrubPreviewSource {
  bool get isAvailable;
  ScrubFrame? getFrame(Duration time);
  void dispose();
}

/// Stacked previews use the whole part layout. Sharing the currently open
/// part alone cannot establish that two versions map item time identically.
bool scrubPreviewServes(MediaSourceInfo loaded, MediaSourceInfo? current) {
  if (current == null) return false;
  final left = loaded.partTimeline?.parts, right = current.partTimeline?.parts;
  final sameSource = loaded.mediaSourceId == current.mediaSourceId;
  if (left != null || right != null) {
    if (left == null || right == null || !sameSource || left.length != right.length) return false;
    for (var i = 0; i < left.length; i++) {
      if (left[i].partId != right[i].partId || left[i].duration != right[i].duration) return false;
    }
    return true;
  }
  if (loaded.partId != null) return loaded.partId == current.partId;
  return current.partId == null && loaded.mediaSourceId != null && sameSource;
}

/// Each part serves frames on its own clock.
class StackedScrubPreviewSource implements ScrubPreviewSource {
  StackedScrubPreviewSource({required this.timeline, required List<ScrubPreviewSource?> sources})
    : assert(sources.length == timeline.parts.length),
      _sources = sources;

  final MediaPartTimeline timeline;
  final List<ScrubPreviewSource?> _sources;

  @override
  bool get isAvailable => _sources.any((source) => source?.isAvailable ?? false);

  @override
  ScrubFrame? getFrame(Duration time) {
    final index = timeline.indexAt(time);
    return _sources[index]?.getFrame(time - timeline.parts[index].start);
  }

  @override
  void dispose() {
    for (final source in _sources) {
      source?.dispose();
    }
  }
}
