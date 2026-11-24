part of 'podcast_sync.dart';

int _actionTime(Map<String, dynamic> action, int received) {
  final timestamp = action['timestamp'];
  if (timestamp == null) {
    return (sourceInt(action['_received']) ?? received) * 1000;
  }
  if (timestamp is! String) {
    throw const SourceException(SourceFailure.invalidData);
  }
  final time = DateTime.tryParse(RegExp(r'(?:Z|[+-]\d{2}:\d{2})$').hasMatch(timestamp) ? timestamp : '${timestamp}Z');
  if (time == null || time.isAfter(DateTime.now().toUtc().add(const Duration(minutes: 5)))) {
    throw const SourceException(SourceFailure.invalidData);
  }
  return time.millisecondsSinceEpoch;
}

PublicationProgress _fromAction(PublicationProgress current, SourceEntry entry, Map<String, dynamic> action) {
  final reset = action['action'] == 'new';
  final position = reset ? 0 : sourceInt(action['position']);
  final total = sourceInt(action['total']) ?? 0;
  if (position == null || position < 0 || position > 7 * 24 * 3600 || total > 7 * 24 * 3600 || total < 0) {
    throw const SourceException(SourceFailure.invalidData);
  }
  final duration = entry.files.firstOrNull?.durationMs ?? total * 1000;
  final milliseconds = duration > 0 ? (position * 1000).clamp(0, duration).toInt() : position * 1000;
  return current.copyWith(
    session: const Uuid().v4(),
    positionMs: milliseconds,
    filePositionMs: milliseconds,
    fileIndex: 0,
    fraction: duration > 0 ? (milliseconds / duration).clamp(0.0, 1.0) : 0,
    finished: !reset && total > 0 && position >= total,
  );
}
