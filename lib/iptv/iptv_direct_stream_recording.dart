part of 'iptv_scheduled_stream_recorder.dart';

extension _IptvDirectStreamRecording on IptvScheduledStreamRecorder {
  Future<bool> _recordDirect(Uri source) async {
    final transport = SourceTransport(allowHttp: allowHttp);
    _transport = transport;
    try {
      final response = await transport.send('GET', source, headers: headers, redirects: true);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        await response.stream.drain<void>();
        SourceTransport.check(response.statusCode);
      }
      final contentType = response.headers['content-type']?.toLowerCase() ?? '';
      if (contentType.contains('mpegurl')) {
        await response.stream.drain<void>();
        return true;
      }
      await _writeDirectStream(response.stream);
      return false;
    } finally {
      transport.close();
      _transport = null;
    }
  }

  Future<void> _writeDirectStream(Stream<List<int>> stream) async {
    final sink = outputFile.openWrite(mode: FileMode.writeOnly);
    final done = Completer<void>();
    _directDone = done;
    var reachedScheduledEnd = false;
    Future<void>? writing;
    late final StreamSubscription<List<int>> subscription;
    void finish([Object? error, StackTrace? stackTrace]) {
      if (done.isCompleted) {
        return;
      }
      _finished = true;
      _onDeadline = null;
      if (error == null) {
        done.complete();
      } else {
        done.completeError(error, stackTrace ?? StackTrace.current);
      }
    }

    Future<void> writeChunk(List<int> bytes) async {
      sink.add(bytes);
      await sink.flush();
    }

    subscription = stream.listen(
      (bytes) {
        subscription.pause();
        final write = writing = writeChunk(bytes);
        unawaited(
          write.then<void>((_) {
            if (!done.isCompleted) subscription.resume();
          }, onError: (Object error, StackTrace stackTrace) => finish(error, stackTrace)),
        );
      },
      onError: (Object error, StackTrace stackTrace) => finish(error, stackTrace),
      onDone: finish,
      cancelOnError: true,
    );
    _subscription = subscription;
    _onDeadline = () {
      reachedScheduledEnd = true;
      _stopped = true;
      unawaited(
        subscription.cancel().then<void>(
          (_) => finish(),
          onError: (Object error, StackTrace stackTrace) {
            finish(error, stackTrace);
          },
        ),
      );
    };
    _armDeadline();
    try {
      await done.future;
      if (!reachedScheduledEnd && !_stopped && _now().isBefore(endsAt)) {
        throw const FileSystemException('Stream ended before the scheduled programme ended');
      }
    } finally {
      _stopTimer?.cancel();
      try {
        // Stop producing chunks before closing the sink. Whether the deadline
        // or the user stopped capture, the chunk already being flushed belongs
        // to this recording.
        await Future.wait<void>([subscription.cancel(), ?writing]);
      } finally {
        _directDone = null;
        _subscription = null;
        await sink.close();
      }
    }
    if (_stopped && await outputFile.length() == 0) {
      throw const FileSystemException('No stream data received');
    }
  }
}
