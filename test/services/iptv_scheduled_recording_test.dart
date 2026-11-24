import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/iptv/iptv_scheduled_stream_recorder.dart';
import 'package:plezy/iptv/iptv_hls_archive.dart';

void main() {
  test('extending a direct recording keeps one HTTP response and all bytes across the original stop', () async {
    final directory = await Directory.systemTemp.createTemp('pleazyng-overtime-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final first = Completer<void>();
    final continueStream = Completer<void>();
    int connections = 0;
    server.listen((request) async {
      connections++;
      request.response.bufferOutput = false;
      request.response.add([1, 2, 3]);
      await request.response.flush();
      first.complete();
      await continueStream.future;
      request.response.add([4, 5, 6]);
      await request.response.flush();
    });
    final output = File('${directory.path}/one.ts');
    final recorder = IptvScheduledStreamRecorder(
      uri: Uri.parse('http://127.0.0.1:${server.port}/live.ts'),
      allowHttp: true,
      outputFile: output,
      endsAt: DateTime.now().add(const Duration(milliseconds: 500)),
    );
    addTearDown(() async {
      if (!continueStream.isCompleted) continueStream.complete();
      await recorder.stop();
      await server.close(force: true);
      await directory.delete(recursive: true);
    });
    final done = recorder.record();
    await first.future;
    recorder.setDeadline(recorder.endsAt.add(const Duration(milliseconds: 550)));
    await Future<void>.delayed(const Duration(milliseconds: 560));
    continueStream.complete();
    await done.timeout(const Duration(seconds: 4));
    expect(recorder.canExtend, isFalse);
    expect(connections, 1);
    expect(await output.readAsBytes(), [1, 2, 3, 4, 5, 6]);
  });

  test('stopping a direct recording waits for its last write before closing the file', () async {
    final directory = await Directory.systemTemp.createTemp('pleazyng-held-recording-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final responseDone = Completer<void>();
    final output = File('${directory.path}/held.ts');
    final held = _HeldWriteFile(output);
    server.listen((request) async {
      request.response.bufferOutput = false;
      request.response.add([1, 2, 3]);
      await request.response.flush();
      await responseDone.future;
    });
    final recorder = IptvScheduledStreamRecorder(
      uri: Uri.parse('http://127.0.0.1:${server.port}/live.ts'),
      allowHttp: true,
      outputFile: held,
      endsAt: DateTime.now().add(const Duration(minutes: 1)),
    );
    addTearDown(() async {
      if (!held.release.isCompleted) held.release.complete();
      responseDone.complete();
      await recorder.stop();
      await server.close(force: true);
      await directory.delete(recursive: true);
    });

    final result = recorder.record().then<Object>((extension) => extension, onError: (Object error) => error);
    await held.started.future;
    await recorder.stop();
    await Future<void>.delayed(Duration.zero);
    held.release.complete();

    expect(await result, '.ts');
    expect(await output.readAsBytes(), [1, 2, 3]);
  });

  test('early direct-stream EOF preserves the captured bytes for salvage', () async {
    final now = DateTime.utc(2026, 1, 1);
    final directory = await Directory.systemTemp.createTemp('plezy-recording-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) async {
      request.response.add(List<int>.filled(188 * 8, 0x47));
      await request.response.close();
    });
    final output = File('${directory.path}/partial.ts');
    final recorder = IptvScheduledStreamRecorder(
      uri: Uri.parse('http://127.0.0.1:${server.port}/channel.ts'),
      allowHttp: true,
      outputFile: output,
      endsAt: now.add(const Duration(minutes: 1)),
      now: () => now,
    );
    addTearDown(() async {
      await recorder.stop();
      await server.close(force: true);
      await directory.delete(recursive: true);
    });
    await expectLater(recorder.record(), throwsA(isA<FileSystemException>()));
    expect(await output.length(), 188 * 8);
  });

  test('HLS continues the existing archive when a segment crosses the original stop', () async {
    final start = DateTime.utc(2026);
    var now = start;
    final directory = await Directory.systemTemp.createTemp('pleazyng-hls-overtime-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final first = Completer<void>(), release = Completer<void>();
    server.listen((req) async {
      if (req.uri.path.endsWith('.m3u8')) {
        req.response.write('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n');
      } else {
        if (req.uri.path == '/a.ts') {
          first.complete();
          await release.future;
        }
        req.response.add(List.filled(188, 0x47));
      }
      await req.response.close();
    });
    final output = File('${directory.path}/one.m3u8');
    final recorder = IptvScheduledStreamRecorder(
      uri: Uri.parse('http://127.0.0.1:${server.port}/live.m3u8'),
      allowHttp: true,
      outputFile: output,
      endsAt: start.add(const Duration(minutes: 1)),
      now: () => now,
    );
    addTearDown(() async {
      if (!release.isCompleted) release.complete();
      await recorder.stop();
      await server.close(force: true);
      await directory.delete(recursive: true);
    });
    final done = recorder.record();
    await first.future;
    recorder.setDeadline(recorder.endsAt.add(const Duration(minutes: 30)));
    now = start.add(const Duration(minutes: 2));
    release.complete();
    await done.timeout(const Duration(seconds: 4));
    expect(await IptvHlsArchive.retainedBytes(output), 376);
  });

  test('scheduled end during an HLS response retains complete segment bytes', () async {
    var now = DateTime.utc(2026, 1, 1);
    final endsAt = now.add(const Duration(seconds: 1));
    final directory = await Directory.systemTemp.createTemp('plezy-hls-recording-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final segmentStarted = Completer<void>();
    final releaseSegment = Completer<void>();
    server.listen((request) async {
      if (request.uri.path == '/live.m3u8') {
        request.response.write('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nsegment.ts\n');
      } else {
        request.response.add(List<int>.filled(188, 0x47));
        await request.response.flush();
        segmentStarted.complete();
        await releaseSegment.future;
        request.response.add(List<int>.filled(188, 0x47));
      }
      await request.response.close();
    });
    final output = File('${directory.path}/partial.ts');
    final recorder = IptvScheduledStreamRecorder(
      uri: Uri.parse('http://127.0.0.1:${server.port}/live.m3u8'),
      allowHttp: true,
      outputFile: output,
      endsAt: endsAt,
      now: () => now,
    );
    addTearDown(() async {
      if (!releaseSegment.isCompleted) releaseSegment.complete();
      await recorder.stop();
      await server.close(force: true);
      await directory.delete(recursive: true);
    });
    final recording = recorder.record();
    await Future.any<void>([
      segmentStarted.future,
      recording.then<void>((_) => fail('Recording finished before the segment request')),
    ]);
    now = endsAt.add(const Duration(milliseconds: 1));
    releaseSegment.complete();
    await recording;
    expect(recorder.fileExtension, '.m3u8');
    expect(await IptvHlsArchive.retainedBytes(output), 188 * 2);
  });
}

/// Simulates one asynchronous filesystem write with the overlap checks of IOSink.
class _HeldWriteFile implements File {
  _HeldWriteFile(this.file);

  final File file;
  final started = Completer<void>();
  final release = Completer<void>();

  @override
  IOSink openWrite({FileMode mode = FileMode.write, Encoding encoding = utf8}) =>
      _HeldWriteSink(file.openWrite(mode: mode, encoding: encoding), started, release);

  @override
  Future<int> length() => file.length();

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _HeldWriteSink implements IOSink {
  _HeldWriteSink(this.sink, this.started, this.release);

  final IOSink sink;
  final Completer<void> started;
  final Completer<void> release;
  bool flushing = false;

  @override
  void add(List<int> data) => sink.add(data);

  @override
  Future<void> flush() async {
    if (flushing) throw StateError('StreamSink is bound to a stream');
    flushing = true;
    if (!started.isCompleted) started.complete();
    try {
      await release.future;
      await sink.flush();
    } finally {
      flushing = false;
    }
  }

  @override
  Future<void> close() => sink.close();

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
