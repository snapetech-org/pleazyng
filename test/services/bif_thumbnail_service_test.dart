import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/services/bif_thumbnail_service.dart';
import 'package:plezy/services/scrub_preview_source.dart';
import 'package:plezy/media/media_part.dart';
import 'package:plezy/media/media_part_timeline.dart';

import '../test_helpers/bif_fixtures.dart';

// BIF parser coverage feeds hand-crafted bytes through the load callback.
// The size limit is enforced while downloading (see
// media_server_http_client_get_bytes_test.dart); download failures are left
// to integration coverage.

void main() {
  group('initial state', () {
    test('isAvailable is false before load()', () {
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      expect(svc.isAvailable, isFalse);
    });

    test('getThumbnail returns null before load()', () {
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      expect(svc.getThumbnail(Duration.zero), isNull);
      expect(svc.getThumbnail(const Duration(seconds: 5)), isNull);
    });
  });

  group('valid BIF parsing', () {
    test('parses a 3-entry BIF with default 1000ms multiplier', () async {
      final bytes = buildBif([
        (timestamp: 0, bytes: [0x10, 0x20]),
        (timestamp: 10, bytes: [0x30, 0x40, 0x50]),
        (timestamp: 20, bytes: [0x60]),
      ]);

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => bytes);

      expect(svc.isAvailable, isTrue);

      // Lookup at exactly the first entry's timestamp.
      expect(svc.getThumbnail(Duration.zero), Uint8List.fromList([0x10, 0x20]));

      // Lookup between entries — should pick the largest <= time.
      // 5s < 10s → first entry.
      expect(svc.getThumbnail(const Duration(seconds: 5)), Uint8List.fromList([0x10, 0x20]));
      // 10s == 2nd entry's timestamp.
      expect(svc.getThumbnail(const Duration(seconds: 10)), Uint8List.fromList([0x30, 0x40, 0x50]));
      // 15s — second entry still.
      expect(svc.getThumbnail(const Duration(seconds: 15)), Uint8List.fromList([0x30, 0x40, 0x50]));
      // 25s past the last entry's timestamp — clamps to last.
      expect(svc.getThumbnail(const Duration(seconds: 25)), Uint8List.fromList([0x60]));
    });

    test('honors a non-default timestampMultiplier', () async {
      // multiplier=500 → each timestamp unit is 500ms.
      final bytes = buildBif([
        (timestamp: 0, bytes: [0xAA]),
        (timestamp: 4, bytes: [0xBB]), // 4 * 500ms = 2000ms
      ], timestampMultiplier: 500);

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => bytes);

      expect(svc.getThumbnail(Duration.zero), Uint8List.fromList([0xAA]));
      // 1.999s — still first entry.
      expect(svc.getThumbnail(const Duration(milliseconds: 1999)), Uint8List.fromList([0xAA]));
      // Exactly 2s — second entry.
      expect(svc.getThumbnail(const Duration(seconds: 2)), Uint8List.fromList([0xBB]));
    });

    test('multiplier=0 is treated as 1000ms (per BIF spec)', () async {
      final bytes = buildBif([
        (timestamp: 0, bytes: [0x01]),
        (timestamp: 7, bytes: [0x02]),
      ], timestampMultiplier: 0);

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => bytes);

      // 7s should hit the second entry (7 * 1000ms).
      expect(svc.getThumbnail(const Duration(seconds: 7)), Uint8List.fromList([0x02]));
    });
  });

  group('malformed BIF input', () {
    test('rejects bytes shorter than the 64-byte header', () async {
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => Uint8List(50));
      expect(svc.isAvailable, isFalse);
    });

    test('rejects bytes with an invalid magic header', () async {
      // 128-byte buffer with all-zero bytes (no magic).
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => Uint8List(128));
      expect(svc.isAvailable, isFalse);
    });

    test('rejects bytes shorter than the declared index table', () async {
      // Build a valid header that claims 100 images but supply no index data.
      final buf = Uint8List(64);
      const magic = [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A];
      for (var i = 0; i < magic.length; i++) {
        buf[i] = magic[i];
      }
      final view = ByteData.sublistView(buf);
      view.setUint32(12, 100, Endian.little); // imageCount=100 → needs 808 more bytes

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => buf);
      expect(svc.isAvailable, isFalse);
    });

    test('skips entries whose offset window is invalid (next <= current)', () async {
      // Build a valid 2-entry BIF, then corrupt the second offset to be inside
      // the first range so `nextImgOffset <= imgOffset` triggers the skip.
      final bytes = buildBif([
        (timestamp: 0, bytes: [0x11, 0x22]),
        (timestamp: 5, bytes: [0x33]),
      ]);
      // Index entry 1 (second image) starts at byte 64+8.
      // Set its imgOffset to a value <= entry 0's imgOffset.
      final view = ByteData.sublistView(bytes);
      // Entry 0's imgOffset is at byte 64+0+4 = 68
      final firstImgOffset = view.getUint32(68, Endian.little);
      // Corrupt entry 1's imgOffset (at 64+8+4=76) to equal firstImgOffset,
      // so for entry 0 the lookahead `nextImgOffset == imgOffset` triggers
      // the `<=` skip path.
      view.setUint32(76, firstImgOffset, Endian.little);

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => bytes);

      // Entry 0 is skipped (next offset == its offset). Entry 1's window is
      // (firstImgOffset .. sentinel.offset), which is still valid, so it
      // remains. Verify exactly one entry survives.
      expect(svc.isAvailable, isTrue);
      // The surviving entry maps timestamp=5*1000ms onwards.
      expect(svc.getThumbnail(const Duration(seconds: 5)), isNotNull);
      // A query before the surviving entry's timestamp clamps to it (since
      // there's only one entry, binary search returns entries[0]).
      expect(svc.getThumbnail(Duration.zero), isNotNull);
    });
  });

  test('a scrub frame can outlive the service without retaining the whole archive', () async {
    final service = BifThumbnailService();
    final archive = buildBif([
      (timestamp: 0, bytes: [42]),
      (timestamp: 1, bytes: [17]),
    ]);
    await service.load((_) async => archive);
    final thumbnail = service.getThumbnail(Duration.zero)!;
    expect(identical(thumbnail, service.getThumbnail(Duration.zero)), isTrue);
    service.dispose();
    expect(thumbnail.buffer.lengthInBytes, thumbnail.length);
    expect(thumbnail, [42]);
  });

  test('empty entries keep the preceding frame and clamp to the first real frame', () async {
    final service = BifThumbnailService();
    addTearDown(service.dispose);
    final archive = buildBif([
      (timestamp: 0, bytes: []),
      (timestamp: 1, bytes: [1]),
      (timestamp: 2, bytes: []),
      (timestamp: 3, bytes: []),
      (timestamp: 4, bytes: [2]),
      (timestamp: 5, bytes: []),
    ]);
    await service.load((_) async => archive);
    for (var i = 0; i <= 6; i++) {
      expect(service.getThumbnail(Duration(seconds: i)), [i < 4 ? 1 : 2]);
    }
  });

  test('stacked files share one byte budget, starting at the playing file', () async {
    final timeline = MediaPartTimeline.fromParts([
      for (var i = 1; i <= 4; i++) MediaPart(id: '$i', durationMs: 10000),
    ], currentIndex: 1)!;
    final requests = <(int, int)>[];
    final size = buildBif([
      (timestamp: 0, bytes: [0]),
    ]).length;
    final source = await BifThumbnailService.loadStacked(timeline, (id, limit) async {
      requests.add((id, limit));
      if (id == 3) return null;
      return buildBif([
        (timestamp: 0, bytes: [id]),
      ]);
    }, maxBytes: size * 2);
    addTearDown(() => source?.dispose());
    expect(requests, [(2, size * 2), (3, size), (4, size)]);
    expect(source!.getFrame(Duration.zero), isNull);
    expect((source.getFrame(const Duration(seconds: 10)) as BytesScrubFrame).bytes, [2]);
    expect(source.getFrame(const Duration(seconds: 20)), isNull);
    expect((source.getFrame(const Duration(seconds: 35)) as BytesScrubFrame).bytes, [4]);
  });

  group('empty input', () {
    test('null download keeps the service in unavailable state', () async {
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => null);
      expect(svc.isAvailable, isFalse);
      expect(svc.getThumbnail(Duration.zero), isNull);
    });

    test('empty bytes keep the service in unavailable state', () async {
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => Uint8List(0));
      expect(svc.isAvailable, isFalse);
    });

    test('a valid BIF with zero images parses but reports unavailable', () async {
      final bytes = buildBif(const []); // no entries, only header + sentinel
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);
      await svc.load((_) async => bytes);

      // Loaded successfully but the entries list is empty — `isAvailable`
      // requires non-empty entries.
      expect(svc.isAvailable, isFalse);
      expect(svc.getThumbnail(Duration.zero), isNull);
    });
  });

  group('reload + dispose', () {
    test('only the latest load can publish and disposal retires outstanding work', () async {
      final svc = BifThumbnailService();
      final old = Completer<Uint8List?>();
      final first = svc.load((_) => old.future);
      final newer = buildBif([
        (timestamp: 0, bytes: [2]),
      ]);
      await svc.load((_) async => newer);
      old.complete(
        buildBif([
          (timestamp: 0, bytes: [1]),
        ]),
      );
      await first;
      expect(svc.getThumbnail(Duration.zero), [2]);
      final pending = Completer<Uint8List?>();
      final last = svc.load((_) => pending.future);
      svc.dispose();
      pending.complete(newer);
      await last;
      expect(svc.isAvailable, isFalse);
    });
    test('index offsets must reference image data, and timestamps must support binary search', () async {
      final service = BifThumbnailService();
      addTearDown(service.dispose);
      final bytes = buildBif([
        (timestamp: 0, bytes: [1]),
      ]);
      ByteData.sublistView(bytes).setUint32(68, 4, Endian.little);
      await service.load((_) async => bytes);
      expect(service.getThumbnail(Duration.zero), isNull);
      await service.load(
        (_) async => buildBif([
          (timestamp: 2, bytes: [1]),
          (timestamp: 1, bytes: [2]),
        ]),
      );
      expect(service.isAvailable, isFalse);
    });
    test('a second load() replaces prior entries', () async {
      final first = buildBif([
        (timestamp: 0, bytes: [0xAA]),
      ]);
      final second = buildBif([
        (timestamp: 0, bytes: [0xBB, 0xCC]),
      ]);

      final svc = BifThumbnailService();
      addTearDown(svc.dispose);

      await svc.load((_) async => first);
      expect(svc.getThumbnail(Duration.zero), Uint8List.fromList([0xAA]));

      await svc.load((_) async => second);
      expect(svc.getThumbnail(Duration.zero), Uint8List.fromList([0xBB, 0xCC]));
    });

    test('a failed reload (null bytes) clears prior entries', () async {
      final first = buildBif([
        (timestamp: 0, bytes: [0xAA]),
      ]);
      final svc = BifThumbnailService();
      addTearDown(svc.dispose);

      await svc.load((_) async => first);
      expect(svc.isAvailable, isTrue);

      // The implementation sets `_entries = null` at the start of `load()`,
      // so a null download leaves the service unavailable.
      await svc.load((_) async => null);
      expect(svc.isAvailable, isFalse);
      expect(svc.getThumbnail(Duration.zero), isNull);
    });

    test('dispose() releases entries', () async {
      final bytes = buildBif([
        (timestamp: 0, bytes: [0xFF]),
      ]);
      final svc = BifThumbnailService();
      await svc.load((_) async => bytes);
      expect(svc.isAvailable, isTrue);

      svc.dispose();
      expect(svc.isAvailable, isFalse);
      expect(svc.getThumbnail(Duration.zero), isNull);
    });
  });
}
