import 'dart:async';
import 'dart:io';

import 'iptv_hls_archive.dart';

import '../sources/source_transport.dart';

part 'iptv_direct_stream_recording.dart';

/// Captures direct HTTP streams and AES-128 or clear HLS while Pleazyng is
/// running. HLS is retained as a self-contained local playlist archive so
/// separate renditions and initialization changes remain intact.
class IptvScheduledStreamRecorder {
  IptvScheduledStreamRecorder({
    required this.uri,
    required this.allowHttp,
    this.headers = const {},
    required this.outputFile,
    required this.endsAt,
    this.requestedStart,
    DateTime Function()? now,
    this.archiveDirectory,
  }) : _now = now ?? DateTime.now;

  final DateTime Function() _now;
  final Uri uri;
  final bool allowHttp;
  final Map<String, String> headers;
  final File outputFile;
  DateTime endsAt;
  final DateTime? requestedStart;
  void Function()? _onDeadline;
  bool _finished = false;

  bool get canExtend => !_stopped && !_finished && (_archive?.capturing ?? true) && _now().isBefore(endsAt);

  // Change only the deadline. Both the HTTP subscription and the HLS archive
  // retain their open files, segment indexes and pending writes.
  void setDeadline(DateTime deadline) {
    endsAt = deadline;
    _archive?.endsAt = deadline;
    _armDeadline();
  }

  void _armDeadline() {
    _stopTimer?.cancel();
    final callback = _onDeadline;
    if (callback != null) _stopTimer = Timer(endsAt.difference(_now()), callback);
  }

  final Directory? archiveDirectory;
  IptvHlsArchive? _archive;
  bool get capturedThroughDeadline => _archive?.complete ?? true;
  Duration? get capturedMediaDuration => _archive?.capturedDuration;
  int get archivedMediaBytes => _archive?.mediaBytes ?? 0;

  SourceTransport? _transport;
  StreamSubscription<List<int>>? _subscription;
  Completer<void>? _directDone;
  Timer? _stopTimer;
  bool _stopped = false;
  String _fileExtension = '.ts';
  String get fileExtension => _fileExtension;

  Future<String> record() async {
    try {
      return await _capture();
    } finally {
      _finished = true;
      _onDeadline = null;
      _stopTimer?.cancel();
    }
  }

  Future<String> _capture() async {
    if (uri.path.toLowerCase().endsWith('.m3u8')) {
      return _recordHls(uri);
    }
    if (await _recordDirect(uri)) {
      return _recordHls(uri);
    }
    final filename = uri.pathSegments.isEmpty ? '' : uri.pathSegments.last;
    final extension = RegExp(
      r'\.(ts|mp2t|m2ts|mp4|m4v|mkv|mp3|aac|m4a|mpg|mpeg|webm)$',
      caseSensitive: false,
    ).firstMatch(filename)?.group(1)?.toLowerCase();
    _fileExtension = extension == null ? '.ts' : '.$extension';
    return _fileExtension;
  }

  Future<void> stop() async {
    _stopped = true;
    _stopTimer?.cancel();
    await _archive?.stop();
    await _subscription?.cancel();
    final directDone = _directDone;
    if (directDone != null && !directDone.isCompleted) {
      directDone.complete();
    }
    _transport?.close();
  }

  Future<String> _recordHls(Uri source) async {
    _fileExtension = '.m3u8';
    final archive = IptvHlsArchive(
      uri: source,
      allowHttp: allowHttp,
      headers: headers,
      output: outputFile,
      directory: archiveDirectory ?? Directory('${outputFile.path}.media'),
      endsAt: endsAt,
      requestedStart: requestedStart,
      now: _now,
    );
    _archive = archive;
    await archive.record();
    return _fileExtension;
  }
}
